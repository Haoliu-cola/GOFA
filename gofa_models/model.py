from collections import namedtuple, OrderedDict
from xml.dom.minidom import Entity

import torch
import numpy as np
import random

from docutils.nodes import target
from numpy.core.defchararray import startswith
# from termcolor import cprint
import re

from gp.nn.models.GNN import MultiLayerMessagePassing
from gp.nn.layer.pyg import RGCNEdgeConv
from gp.nn.layer.pyg import TransformerConv as MConv
from .helper import GOFALlamaHelper, GOFAMistralHelper, LlamaHelper

LLM_DIM_DICT = {"ST": 768, "BERT": 768, "e5": 1024, "llama2_7b": 4096, "llama2_13b": 5120, "mamba": 768, "icae": 4096,
                "icae_mem": 4096}

def print_fixed_length(text, line_width=120):
    t_pointer = 0
    while t_pointer < len(text):
        t_pointer += line_width
        t_cur_text = text[t_pointer - line_width:t_pointer]
        print(t_cur_text)


def print_text_side_by_side(text_1, text_2, line_width=120, space=10):
    t1_len = len(text_1)
    t2_len = len(text_2)
    seg_width = int((line_width-space)/2)
    t1_pointer = 0
    t2_pointer = 0
    text_1 = text_1.replace('\n', ' ').replace('\r', ' ')
    text_2 = text_2.replace('\n', ' ').replace('\r', ' ')
    while t1_pointer<t1_len or t2_pointer<t2_len:
        t1_pointer += seg_width
        t2_pointer += seg_width
        t1_cur_text = text_1[t1_pointer-seg_width:t1_pointer]
        t2_cur_text = text_2[t2_pointer-seg_width:t2_pointer]
        t1_cur_text = t1_cur_text + " "*(seg_width-len(t1_cur_text))
        t2_cur_text = t2_cur_text + " "*(seg_width-len(t2_cur_text))
        full_text = t1_cur_text + " "*space + t2_cur_text
        print(full_text)

def find_one_hop_neighbors(edge_index, target_index):
    source_nodes = edge_index[0]
    destination_nodes = edge_index[1]
    # Find all nodes that are connected to the target (incoming and outgoing edges)
    one_hop_neighbors = np.unique(
        np.concatenate([
            source_nodes[destination_nodes == target_index],  # Incoming edges to target
            destination_nodes[source_nodes == target_index]  # Outgoing edges from target
        ])
    )
    return one_hop_neighbors

def get_neighbor_text(g, target_index, truncate_len):
    question_index = g.node_map[g.question_index.cpu().numpy()].cpu().numpy()
    exl_index = target_index.tolist() + question_index.tolist()
    neighbor_index = g.node_map[
        ~np.isin(g.node_map.cpu().numpy(), exl_index)].cpu().numpy()
    neighbor_texts = g.x[neighbor_index].tolist()
    neighbor_texts = [' '.join(t.split()[:truncate_len]) for t in neighbor_texts]
    return neighbor_texts

def get_edge_text(g, edge_index, node_ids):
    # filter the prompt edges
    edge_index = g.edge_index.cpu().numpy()[:, :-2 * len(g.target_index) * len(g.question)]
    for i in range(len(node_ids)):
        cur_id = node_ids[i]
        for t in g.x:
            if cur_id in t:
                cur_name = t[t.find('Name: ')+6:t.find(';')]
                node_ids[i] = cur_name
    edge_text = node_ids[edge_index]
    edge_map = g.edge_map[:len(edge_index[0])].cpu().numpy()
    for i in range(len(g.edge_attr)):
        g.edge_attr[i] = ' is' + g.edge_attr[i][g.edge_attr[i].find(':')+1:] + ' '
    new_edge_attr = g.edge_attr[edge_map]
    edge_text = ', '.join(edge_text[0] + new_edge_attr + edge_text[1])
    return edge_text + '. '



class GOFA(torch.nn.Module):
    def __init__(self, transformer_args, mode="autoencoder", base_llm="llama7b", save_dir=""):
        super().__init__()

        self.mode = mode
        self.save_dir = save_dir

        if base_llm == 'llama7b':
            self.llm_model = GOFALlamaHelper(transformer_args)
        elif base_llm == 'mistral7b':
            self.llm_model = GOFAMistralHelper(transformer_args)
        elif base_llm == 'llama7blora' or 'mistral7blora':
            self.llm_model = LlamaHelper(transformer_args)
        else:
            raise NotImplementedError(base_llm + " is not supported. Please choose from: llama7b, mistral7b,")

        if mode == "autoencoder":
            self.encode = self.auto_encode
            self.decode = self.auto_decode
        elif mode == "autoencodergen":
            self.encode = self.auto_encode
            self.decode = self.auto_generate
        elif mode == "direct":
            self.encode = self.direct_decode
            self.decode = lambda x: x
        elif mode == "nograph":
            self.encode = self.llm_decode
            self.decode = lambda x: x
        elif mode == "nographft":
            self.encode = lambda x: x
            self.decode = self.llm_ft
        elif mode == "nographgen":
            self.encode = lambda x: x
            self.decode = self.llm_gen_neighbor
        else:
            # TODO: not implemented
            raise NotImplementedError(mode + " mode not implemented")

    def llm_decode(self, g):
        # For finetuning LLM using sentence completion task
        text_inputs = g.x[g.node_map.cpu().numpy()][g.question_index.cpu().numpy()].tolist()
        com_text_inputs = []
        for i in range(len(text_inputs)):
            t = text_inputs[i] + g.question[i]
            com_text_inputs.append(t)
        answer_texts = g.answer[g.answer_map.cpu().numpy()].tolist()

        answer_logits, answer_id, masks = self.llm_model(answer_texts, com_text_inputs)
        GNNLMOutput = namedtuple("GNNLMOutput", ["logits", "answer_id", "pred_text", "answer"])
        return GNNLMOutput(logits=answer_logits[masks], pred_text=self.logit_to_text(answer_logits, masks),
                           answer_id=answer_id, answer=answer_texts)

    def llm_ft(self, g):
        truncate_len = 256
        target_index = g.node_map[g.target_index.cpu().numpy()].cpu().numpy()
        text_inputs = g.x[target_index].tolist()
        text_inputs = [' '.join(t.split()[:truncate_len]) for t in
                       text_inputs + ['These nodes have following neighbor nodes'] + get_neighbor_text(g, target_index, truncate_len) ]
        text_inputs = '. '.join(text_inputs)
        text_inputs += get_edge_text(g, g.edge_index.cpu().numpy(), g.node_ids[0])

        com_text_inputs = []
        for i in range(len(g.question)):
            t = text_inputs + g.question[i]
            com_text_inputs.append(t)
        answer_texts = g.answer[g.answer_map.cpu().numpy()].tolist()

        answer_logits, answer_id, masks = self.llm_model(answer_texts, com_text_inputs)
        GNNLMOutput = namedtuple("GNNLMOutput", ["logits", "answer_id", "pred_text", "answer"])
        return GNNLMOutput(logits=answer_logits[masks], pred_text=self.logit_to_text(answer_logits, masks),
                           answer_id=answer_id, answer=answer_texts)

    def llm_gen_neighbor(self, g):
        truncate_len = 512
        target_index = g.node_map[g.target_index.cpu().numpy()].cpu().numpy()
        text_inputs = g.x[target_index].tolist()
        text_inputs = [' '.join(t.split()[:truncate_len]) for t in
                       text_inputs + ['These nodes have following neighbor nodes'] + get_neighbor_text(g, target_index, truncate_len)]
        text_inputs = '. '.join(text_inputs)
        text_inputs += get_edge_text(g, g.edge_index.cpu().numpy(), g.node_ids[0])

        com_text_inputs = []

        # TODO: current version only support len(g.question)==1
        for i in range(len(g.question)):
            if len(g.target_index) == len(g.question):
                t = '[INST]\n' + text_inputs + g.question[i] + ' Please only strictly answer one category name, no other information or explanation. \n[/INST]\n\n'
            elif len(g.target_index) == len(g.question) * 2:
                t = '[INST]\n' + text_inputs + g.question[
                    i] + ' Please only strictly answer yes or no, no other information or explanation. Do not add anything else. \n[/INST]\n\n'
            elif len(g.target_index) > len(g.question):
                t = '[INST]\n' + text_inputs + g.question[i] + ' Please directly answer the question, no other information or explanation. \n[/INST]\n\n'
            else:
                raise ValueError('Text_input length did not match question length.')
            com_text_inputs.append(t)
        answer_texts = g.answer[g.answer_map.cpu().numpy()].tolist()
        generated_text = self.llm_model.generate(com_text_inputs)
        for i, txt in enumerate(generated_text):
            print_fixed_length(f"question: {com_text_inputs}")
            print("-" * 120)
            print_text_side_by_side("target: " + answer_texts[i], "gen: " + generated_text[i])
            print("=" * 120)
        GNNLMOutput = namedtuple("GNNLMOutput", ["logits", "answer_id", "pred_text", "answer"])
        return GNNLMOutput(logits=torch.randn([1, 32132]), pred_text=generated_text, answer_id=torch.tensor([1]),
                           answer=answer_texts)

    def auto_encode(self, g):
        g.num_node_feat = g.x.shape[0]
        if g.edge_attr is not None:
            text_inputs = np.concatenate([g.x, g.edge_attr], axis=0)
        else:
            text_inputs = g.x
        llm_output = self.llm_model.encode(text_inputs.tolist(), graph=g, partial_grad=True)
        g.x = llm_output[:g.node_map.size(-1)]
        return g

    def auto_decode(self, g):
        emb = g.x
        answer_texts = g.answer[g.answer_map.cpu().numpy()].tolist()
        prompt_texts = g.question[g.question_map.cpu().numpy()].tolist()
        prompt_texts = ["" if p.startswith("Please complete the sentence") else p for p in prompt_texts]
        emb = emb[g.question_index]
        answer_logits, answer_id, masks = self.llm_model.decode(answer_texts, emb, prompt=prompt_texts)
        GNNLMOutput = namedtuple("GNNLMOutput", ["logits", "answer_id", "pred_text", "answer"])
        return GNNLMOutput(logits=answer_logits[masks][:,:32000], pred_text=self.logit_to_text(answer_logits, masks),
                           answer_id=answer_id, answer=answer_texts)

    def auto_generate(self, g):
        emb = g.x
        answer_texts = g.answer[g.answer_map.cpu().numpy()].tolist()
        prompt_texts = g.question[g.question_map.cpu().numpy()].tolist()
        prompt_texts = ["" if p.startswith("Please complete the sentence") else p for p in prompt_texts]
        emb = emb[g.question_index]
        generated_text = self.llm_model.generate(emb, prompt=prompt_texts)
        for i, txt in enumerate(generated_text):
            print_fixed_length("question: " + prompt_texts[i])
            print("-"*120)
            print_text_side_by_side("target: "+answer_texts[i], "gen: "+generated_text[i])
            print("="*120)
        GNNLMOutput = namedtuple("GNNLMOutput", ["logits", "answer_id", "pred_text", "answer"])
        return GNNLMOutput(logits=torch.randn([1, 32132]).to(emb.device), pred_text=generated_text, answer_id=torch.tensor([1]).to(emb.device),
                           answer=answer_texts)

    def forward(self, g):
        g = self.encode(g)
        return self.decode(g)

    def direct_decode(self, g):
        num_node_feat = g.x.shape[0]
        g.num_node_feat = num_node_feat
        g.x = g.x[g.node_map.cpu().numpy()]
        answer_texts = g.answer.tolist()
        answer_logits, answer_id, masks = self.llm_model(g.x.tolist(), answer_texts, g.edge_attr.tolist(), graph=g,
                                                         partial_grad=True)
        GNNLMOutput = namedtuple("GNNLMOutput", ["logits", "answer_id", "pred_text", "answer"])
        return GNNLMOutput(logits=answer_logits[masks], pred_text=self.logit_to_text(answer_logits, masks),
                           answer_id=answer_id, answer=answer_texts)

    def save_partial(self, save_dir):
        if self.mode.startswith('nograph'):
            state_dict = OrderedDict()
        else:
            state_dict = self.llm_model.model.icae.model.model.g_layers.state_dict()
        full_state_dict = self.state_dict()
        for k in full_state_dict:
            if "default" in k:
                state_dict[k] = full_state_dict[k]
        torch.save(state_dict, save_dir)

    def load_partial(self, load_dir=None, state_dict=None):
        if load_dir is not None and state_dict is not None:
            raise RuntimeError("You should only specify either load_dict or load_dir")
        if load_dir is None and state_dict is None:
            print("No state dict loaded")
            return
        if load_dir is not None:
            state_dict = torch.load(load_dir)
        new_state_dict = OrderedDict()
        for name in state_dict:
            if "decadapt" in name:
                new_state_dict[name.replace("decadapt", "default")] = state_dict[name]
            else:
                new_state_dict[name] = state_dict[name]
        self.llm_model.model.icae.model.model.g_layers.load_state_dict(new_state_dict, strict=False)
        self.load_state_dict(new_state_dict, strict=False)

    def logit_to_text(self, logits, masks):
        tokenizer = self.llm_model.get_tokenizer()
        if len(logits.size()) == 2:
            logits = logits.unsqueeze(0)
        decoded_texts = []
        for i in range(logits.size(0)):
            sample_logit = logits[i]
            sample_mask = masks[i]
            sample_logit = sample_logit[sample_mask]
            token_ids = sample_logit[:, :32000].argmax(dim=-1).unsqueeze(0)
            token_ids[token_ids >= 32000] = 1
            sample_text = tokenizer.batch_decode(token_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True)
            decoded_texts.extend(sample_text)
        return decoded_texts


class PyGRGCNEdge(MultiLayerMessagePassing):
    def __init__(self, num_layers: int, num_rels: int, inp_dim: int, out_dim: int, drop_ratio=0, JK="last",
                 batch_norm=True, ):
        super().__init__(num_layers, inp_dim, out_dim, drop_ratio, JK, batch_norm)
        self.num_rels = num_rels
        self.build_layers()

    def build_input_layer(self):
        return RGCNEdgeConv(self.inp_dim, self.out_dim, self.num_rels)

    def build_hidden_layer(self):
        return RGCNEdgeConv(self.inp_dim, self.out_dim, self.num_rels)

    def build_message_from_input(self, g):
        return {"g": g.edge_index, "h": g.x, "e": g.edge_type, "he": g.edge_attr, }

    def build_message_from_output(self, g, h):
        return {"g": g.edge_index, "h": h, "e": g.edge_type, "he": g.edge_attr}

    def layer_forward(self, layer, message):
        return self.conv[layer](message["h"], message["he"], message["g"], message["e"])


class PyGRGATCN(MultiLayerMessagePassing):
    def __init__(self, num_layers: int, t_layer: int, inp_dim: int, out_dim: int, drop_ratio=0.0, JK="last",
                 batch_norm=True, ):
        super().__init__(num_layers, inp_dim, out_dim, 0, JK, batch_norm)
        self.t_layer = t_layer
        self.layer_dropout = drop_ratio
        self.build_layers()

    def build_input_layer(self):
        # return GATConv(self.inp_dim*self.t_layer, self.inp_dim*self.t_layer)
        # return TransformerConv(self.inp_dim*self.t_layer, self.inp_dim*self.t_layer)
        return MConv(self.inp_dim, self.t_layer, 8, add_self_loops=True, dropout=self.layer_dropout,
                     layer_idx=len(self.conv))

    def build_hidden_layer(self):
        # return GATConv(self.inp_dim * self.t_layer, self.inp_dim * self.t_layer)
        # return TransformerConv(self.inp_dim*self.t_layer, self.inp_dim*self.t_layer)
        return MConv(self.inp_dim, self.t_layer, 8, add_self_loops=True, dropout=self.layer_dropout,
                     layer_idx=len(self.conv))

    def build_message_from_input(self, g):
        return {"g": g.edge_index, "h": g.x, "e": g.edge_type, "he": g.edge_attr, }

    def build_message_from_output(self, g, h):
        return {"g": g.edge_index, "h": h, "e": g.edge_type, "he": g.edge_attr}

    def layer_forward(self, layer, message):
        return self.conv[layer](message["h"], message["g"], message["he"])
