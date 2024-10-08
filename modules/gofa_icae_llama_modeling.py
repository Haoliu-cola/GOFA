# Major change since July 9 for scaling up
# Major change since July 18 for fixing the lora bug
from transformers import LlamaTokenizer
import torch
import torch.nn as nn
import types
from dataclasses import dataclass, field
from typing import Optional
from peft import (get_peft_model, LoraConfig)
from torch.nn.functional import gelu
from modules.gofa_modeling import GOFALlamaForCausalLM
from transformers import BitsAndBytesConfig

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


@dataclass
class ModelArguments:
    model_name_or_path: str = field(default="meta-llama/Llama-2-7b-chat-hf")
    memory_head: bool = field(default=False, metadata={"help": "whether to add a memory head for the encoder."})
    better_transformer: bool = field(default=False,
                                     metadata={"help": "whether to enable bettertransformer for flash attention."})
    mem_size: int = field(default=128, metadata={"help": "Memory size"}, )
    lora_r: int = field(default=128, metadata={"help": "lora rank"})
    lora_dropout: float = field(default=0.05, metadata={"help": "lora dropout"})
    quantization: bool = field(default=False, metadata={"help": "quantization"})
    dec_lora: bool = field(default=False, metadata={"help": "decoder lora"})


@dataclass
class DataArguments:
    data_path: str = field(default=None, metadata={"help": "Path to the training data."})
    debug_data: bool = field(default=False,
                             metadata={"help": "Enable debug dataset to quickly verify the training process"})


@dataclass
class TrainingArguments:
    cache_dir: Optional[str] = field(default=None)
    optim: str = field(default="adamw_torch")
    model_max_length: int = field(default=512, metadata={
        "help": "Maximum sequence length. Sequences will be right padded (and possibly truncated)."}, )
    per_device_train_batch_size: int = field(default=16, metadata={"help": "Per device train batch size."}, )
    bf16: bool = field(default=False, metadata={"help": "Use brain float 16"}, )
    lm_ratio: float = field(default=0.0, metadata={"help": "Ratio for LM training."}, )
    restore_from: str = field(default="",
                              metadata={"help": "The checkpoint that should be restored from for fine-tuning"})


def print_trainable_parameters(model):
    trainable_parameters = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_parameters += param.numel()
    print(f"trainable params: {trainable_parameters} || all params: {all_param} || trainable%: "
          f"{100 * trainable_parameters / all_param}")
    for name, param in model.named_parameters():
        if param.requires_grad:
            print(name, param.shape)


class MemoryHead(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dense_in = nn.Linear(dim, dim)
        self.dense_out = nn.Linear(dim, dim)

    def forward(self, x):
        previous_type = x.dtype
        x = x.to(self.dense_in.weight.dtype)
        x = self.dense_in(x)
        x = gelu(x)
        return self.dense_out(x).to(previous_type)


class LlamaICAE(nn.Module):
    def __init__(self, model_args, training_args, gofa_config):
        super().__init__()
        self.model_args = model_args
        self.training_args = training_args
        self.model_name = model_args.model_name_or_path
        # self.auto_encoder = AutoModelForCausalLM.from_pretrained(model_name).to(device)
        self.quantization = model_args.quantization
        if self.quantization:
            bnb_config = self.create_bnb_config()
            self.icae = GOFALlamaForCausalLM.from_pretrained(self.model_name, gofa_config, quantization_config=bnb_config)
        else:
            self.icae = GOFALlamaForCausalLM.from_pretrained(self.model_name, gofa_config,
                                                         torch_dtype=torch.float16 if training_args.bf16 is False
                                                         else torch.bfloat16)
        # self.dec = LlamaForCausalLM.from_pretrained(self.model_name,
        #                                             torch_dtype=torch.float16 if training_args.bf16 is False else
        #                                             torch.bfloat16)
        self.vocab_size = self.icae.config.vocab_size + 1  # [PAD] token
        self.icae.resize_token_embeddings(self.vocab_size + model_args.mem_size + 3)
        self.pad_token_id = self.vocab_size - 1

        # tunable
        self.ae_token_id = self.vocab_size + model_args.mem_size
        self.lm_token_id = self.vocab_size + model_args.mem_size + 1
        self.ft_token_id = self.vocab_size + model_args.mem_size + 2

        self.eos_id = 1
        self.dim = self.icae.config.hidden_size
        # if self.quantization:
        #     self.icae = prepare_model_for_kbit_training(self.icae)
        lora_config = self.create_lora_config()

        if self.model_args.dec_lora:
            dec_lora_config = self.create_dec_lora_config()
            self.icae = get_peft_model(self.icae, dec_lora_config, adapter_name="default")
            self.icae.add_adapter("encadapt", lora_config)
            self.icae.set_adapter("default")
        else:
            self.icae = get_peft_model(self.icae, lora_config, adapter_name="encadapt")
            self.icae.set_adapter("encadapt")
        for name, param in self.icae.named_parameters():
            if "g_layers" in name:
                param.requires_grad = True
        self.memory_token_embed = nn.Embedding(model_args.mem_size + 3, self.dim, padding_idx=None)
        self.loss_fct = nn.CrossEntropyLoss(ignore_index=-100)
        self.tokenizer = LlamaTokenizer.from_pretrained(self.model_name)
        self.left_tokenizer = LlamaTokenizer.from_pretrained(self.model_name)
        self.left_tokenizer.padding_side = "left"
        self.left_tokenizer.truncation_side = "left"

        self.memory_head = MemoryHead(self.dim) if model_args.memory_head else None
        self.loss_denominator = training_args.per_device_train_batch_size * training_args.model_max_length

    def forward(self, input_ids: torch.LongTensor = None, prompt_answer_ids: torch.LongTensor = None,
                labels: Optional[torch.LongTensor] = None, token_avg_loss: bool = False):
        batch_size = input_ids.size(0)
        memory_mask = input_ids >= self.vocab_size

        autoencoder_input_embedding = self.icae.get_base_model().model.embed_tokens(input_ids)
        autoencoder_input_embedding[memory_mask] = self.memory_token_embed(input_ids[memory_mask] - self.vocab_size).to(
            autoencoder_input_embedding)

        compress_outputs = self.icae(inputs_embeds=autoencoder_input_embedding, output_hidden_states=True,
                                     enable_lora=True)
        compress_outputs = compress_outputs.hidden_states[-1] if self.memory_head is None else self.memory_head(
            compress_outputs.hidden_states[-1])

        # get the last k hidden states
        memory_embedding = compress_outputs[memory_mask].view(batch_size, self.model_args.mem_size, -1)
        prompt_answer_embs = self.icae.get_base_model().model.embed_tokens(prompt_answer_ids)
        special_prompt = prompt_answer_ids >= self.vocab_size
        prompt_answer_embs[special_prompt] = self.memory_token_embed(
            prompt_answer_ids[special_prompt] - self.vocab_size).to(prompt_answer_embs)
        decoder_input_embeddings = torch.cat((memory_embedding, prompt_answer_embs), dim=1)
        decoder_outputs = self.icae(inputs_embeds=decoder_input_embeddings, output_hidden_states=True)

        logits = decoder_outputs.logits[:, self.model_args.mem_size - 1:-1, :].contiguous()
        assert (labels < self.vocab_size).all()
        logits = logits.view(-1, logits.size(-1))
        target_ids = labels.view(-1)
        loss = self.loss_fct(logits, target_ids)

        if token_avg_loss:
            numer = target_ids.ne(-100).sum()
            loss *= numer / self.loss_denominator
            return {"loss": loss, "logits": logits.view(batch_size, -1, logits.size(-1)),
                    "loss_denominator": self.loss_denominator}
        else:
            return {"loss": loss, "logits": logits.view(batch_size, -1, logits.size(-1))}

    def create_bnb_config(self):
        """
        quantization configuration.
        """
        bnb_config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4",
                                        bnb_4bit_compute_dtype=torch.bfloat16)
        # bnb_config = BitsAndBytesConfig(load_in_8bit=True)

        return bnb_config

    def create_lora_config(self):
        lora_config = LoraConfig(

            r=self.model_args.lora_r,

            lora_alpha=32,

            lora_dropout=self.model_args.lora_dropout,

            bias="none",

            task_type="CAUSAL_LM"

        )
        return lora_config

    def create_dec_lora_config(self):
        lora_config = LoraConfig(

            r=32,

            lora_alpha=32,

            lora_dropout=0.05,

            bias="none",

            task_type="CAUSAL_LM"

        )
        return lora_config

    def merge_lora(self):
        self.icae = self.icae.merge_and_unload()

        def re_self(self):
            return self

        def null_func(self):
            return None

        self.icae.get_base_model = types.MethodType(re_self, self.icae)
        self.icae.enable_adapter_layers = types.MethodType(null_func, self.icae)
        self.icae.disable_adapter_layers = types.MethodType(null_func, self.icae)
