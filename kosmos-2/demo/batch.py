#!/usr/bin/env python3 -u
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""
Translate raw text with a trained model. Batches data on-the-fly.
"""
import sys
sys.path.append( '.' )

import unilm

import ast
import logging
import math
import os
import glob
import sys
import time
import re
import random
from argparse import Namespace
from collections import namedtuple

import numpy as np
import torch

from fairseq import checkpoint_utils, distributed_utils, options, tasks, utils
from fairseq.dataclass.configs import FairseqConfig
from fairseq.dataclass.utils import convert_namespace_to_omegaconf
from fairseq.token_generation_constraints import pack_constraints, unpack_constraints
from fairseq_cli.generate import get_symbols_to_strip_from_output

import sentencepiece as spm
from torchvision import transforms
from PIL import Image

result_output_dir = None

# This is simple maximum entropy normalization performed in Inception paper
inception_normalize = transforms.Compose(
    [transforms.Normalize(mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711])]
)

def square_transform(size=224):
    return transforms.Compose(
        [
            transforms.Resize((size, size), interpolation=transforms.InterpolationMode.BICUBIC),
            transforms.ToTensor(),
            inception_normalize,
        ]
    )

def split_string(string, separators):
    """
    Function to split a given string based on a list of separators.

    Args:
    string (str): The input string to be split.
    separators (list): A list of separators to be used for splitting the string.

    Returns:
    A list containing the split string with separators included.
    """
    pattern = "|".join(re.escape(separator) for separator in separators) 
    result = re.split(f'({pattern})', string)  
    return [elem for elem in result if elem] 

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    stream=sys.stdout,
)
logger = logging.getLogger("fairseq_cli.interactive")


Batch = namedtuple("Batch", "ids src_tokens src_lengths constraints img_src_tokens img_gpt_input_mask")
Translation = namedtuple("Translation", "src_str hypos pos_scores alignments")

def get_interactive_tokens_and_lengths(self, lines, encode_fn, tokenizer=None):
    """
    line format: [image]path<tab>text<tab>[image]path
    model input: `<s> <image> image hidden </image> My cat looking very dignified.</s>`
    """
    image_feature_length = self.args.image_feature_length
    bos_id = self.dictionary.bos()
    eos_id = self.dictionary.eos()
    boi_id = self.dictionary.index("<image>")
    eoi_id = self.dictionary.index("</image>")
    
    def convert_one_line(input_str):
        # TODO: input interleave image and text
        token = []
        img_src_token = []
        img_gpt_input_mask = []
        segments = input_str.split('<tab>')
        token.append(bos_id)
        img_gpt_input_mask.append(0)
        for i, segment in enumerate(segments):
            if segment.startswith('[image]'):
                image_path = segment[7:]
                # read image and transform to tensor
                image = Image.open(image_path).convert("RGB")
                # update the global_path
                image_tensor = square_transform(self.args.input_resolution)(image)
                img_src_token.append(image_tensor)
                token.extend([boi_id] + list(range(4, image_feature_length+4)) + [eoi_id])
                
                img_gpt_input_mask.extend([0] + [1] * image_feature_length + [0])
            else:
                special_tokens = [self.source_dictionary[idx] for idx in range(tokenizer.vocab_size(), 
                                                                               len(self.source_dictionary))]
                split_special_token_words = []
                split_resutls = split_string(segment, special_tokens)
                for string in split_resutls:
                    if string in special_tokens:
                        # print(f"dict-length({len(self.source_dictionary)}), substring {string} is a special token")
                        split_special_token_words.append(string)
                    else:
                        encode_tokens = tokenizer.encode(string, out_type=str)
                        # print(f"dict-length({len(self.source_dictionary)}), substring {string} is not a special token, tokenized into {encode_tokens}")
                        split_special_token_words.extend(encode_tokens)
                segment = ' '.join(split_special_token_words)
                
                text_tokens = self.source_dictionary.encode_line(
                    encode_fn(segment), add_if_not_exist=False
                ).tolist()
                
                text_tokens = text_tokens[:-1] # </s> in token
                token.extend(text_tokens)
                img_gpt_input_mask.extend([0] * (len(text_tokens))) # </s> in token
        token.append(eos_id)
        # img_gpt_input_mask = img_gpt_input_mask[:-1]
        assert len(token) == len(img_gpt_input_mask) + 1 
        token = torch.LongTensor(token)
        img_gpt_input_mask = torch.LongTensor(img_gpt_input_mask)
        img_src_token = torch.stack(img_src_token, dim=0)
        return token, img_src_token, img_gpt_input_mask
    
    tokens = []
    img_src_tokens = []
    img_gpt_input_masks = []
    for src_str in lines:
        token, img_src_token, img_gpt_input_mask = convert_one_line(src_str)
        tokens.append(token)
        img_src_tokens.append(img_src_token)
        img_gpt_input_masks.append(img_gpt_input_mask)
    lengths = [t.numel() for t in tokens]
    
    return tokens, lengths, img_src_tokens, img_gpt_input_masks


def make_batches(lines, cfg, task, max_positions, encode_fn):
    def encode_fn_target(x):
        return encode_fn(x)

    if cfg.generation.constraints:
        # Strip (tab-delimited) contraints, if present, from input lines,
        # store them in batch_constraints
        batch_constraints = [list() for _ in lines]
        for i, line in enumerate(lines):
            if "\t" in line:
                lines[i], *batch_constraints[i] = line.split("\t")

        # Convert each List[str] to List[Tensor]
        for i, constraint_list in enumerate(batch_constraints):
            batch_constraints[i] = [
                task.target_dictionary.encode_line(
                    encode_fn_target(constraint),
                    append_eos=False,
                    add_if_not_exist=False,
                )
                for constraint in constraint_list
            ]

    if cfg.generation.constraints:
        constraints_tensor = pack_constraints(batch_constraints)
    else:
        constraints_tensor = None

    tokenizer = spm.SentencePieceProcessor()
    if os.path.exists('data/sentencepiece.bpe.model'):
        tokenizer.Load('data/sentencepiece.bpe.model')
    else:
        tokenizer = None
    tokens, lengths, img_src_tokens, img_gpt_input_mask = get_interactive_tokens_and_lengths(task, lines, encode_fn, tokenizer)

    itr = task.get_batch_iterator(
        dataset=task.build_dataset_for_caption_inference(
            tokens, lengths, img_src_tokens, img_gpt_input_mask, constraints=constraints_tensor
        ),
        max_tokens=cfg.dataset.max_tokens,
        max_sentences=cfg.dataset.batch_size,
        max_positions=max_positions,
        ignore_invalid_inputs=cfg.dataset.skip_invalid_size_inputs_valid_test,
    ).next_epoch_itr(shuffle=False)
    for batch in itr:
        ids = batch["id"]
        src_tokens = batch["net_input"]["src_tokens"]
        src_lengths = batch["net_input"]["src_lengths"]
        img_src_tokens = batch["net_input"]["img_src_tokens"]
        img_gpt_input_mask = batch["net_input"]["img_gpt_input_mask"]
        constraints = batch.get("constraints", None)

        yield Batch(
            ids=ids,
            src_tokens=src_tokens,
            src_lengths=src_lengths,
            img_src_tokens=img_src_tokens,
            img_gpt_input_mask=img_gpt_input_mask,
            constraints=constraints,
        )


def main(cfg: FairseqConfig, image_path:str="", output_path:str=""):
    if isinstance(cfg, Namespace):
        cfg = convert_namespace_to_omegaconf(cfg)

    utils.import_user_module(cfg.common)

    if cfg.interactive.buffer_size < 1:
        cfg.interactive.buffer_size = 1
    if cfg.dataset.max_tokens is None and cfg.dataset.batch_size is None:
        cfg.dataset.batch_size = 1

    assert (
        not cfg.generation.sampling or cfg.generation.nbest == cfg.generation.beam
    ), "--sampling requires --nbest to be equal to --beam"
    assert (
        not cfg.dataset.batch_size
        or cfg.dataset.batch_size <= cfg.interactive.buffer_size
    ), "--batch-size cannot be larger than --buffer-size"

    logger.info(cfg)

    # Fix seed for stochastic decoding
    if cfg.common.seed is not None and not cfg.generation.no_seed_provided:
        np.random.seed(cfg.common.seed)
        utils.set_torch_seed(cfg.common.seed)

    use_cuda = torch.cuda.is_available() and not cfg.common.cpu

    # Setup task, e.g., translation
    logger.info("Task: {}".format(cfg.task))
    task = tasks.setup_task(cfg.task)

    # Load ensemble
    overrides = ast.literal_eval(cfg.common_eval.model_overrides)
    logger.info("loading model(s) from {}".format(cfg.common_eval.path))
    models, _model_args = checkpoint_utils.load_model_ensemble(
        utils.split_paths(cfg.common_eval.path),
        arg_overrides=overrides,
        task=task,
        suffix=cfg.checkpoint.checkpoint_suffix,
        strict=(cfg.checkpoint.checkpoint_shard_count == 1),
        num_shards=cfg.checkpoint.checkpoint_shard_count,
    )

    # Set dictionaries
    src_dict = task.source_dictionary
    tgt_dict = task.target_dictionary

    # Optimize ensemble for generation
    for model in models:
        if model is None:
            continue
        if cfg.common.fp16:
            model.half()
        if use_cuda and not cfg.distributed_training.pipeline_model_parallel:
            model.cuda()
        model.prepare_for_inference_(cfg)

    # Initialize generator
    generator = task.build_generator(models, cfg.generation)

    # Handle tokenization and BPE
    tokenizer = task.build_tokenizer(cfg.tokenizer)
    bpe = task.build_bpe(cfg.bpe)

    def encode_fn(x):
        if tokenizer is not None:
            x = tokenizer.encode(x)
        if bpe is not None:
            x = bpe.encode(x)
        return x

    def decode_fn(x):
        if bpe is not None:
            x = bpe.decode(x)
        if tokenizer is not None:
            x = tokenizer.decode(x)
        return x

    # Load alignment dictionary for unknown word replacement
    # (None if no unknown word replacement, empty if no path to align dictionary)
    align_dict = utils.load_align_dict(cfg.generation.replace_unk)

    max_positions = utils.resolve_max_positions(
        task.max_positions(), *[model.max_positions() for model in models]
    )

    if cfg.generation.constraints:
        logger.warning(
            "NOTE: Constrained decoding currently assumes a shared subword vocabulary."
        )

    if cfg.interactive.buffer_size > 1:
        logger.info("Sentence buffer size: %s", cfg.interactive.buffer_size)
    logger.info("NOTE: hypothesis and token scores are output in base 2")
    logger.info("Type the input sentence and press return:")
    start_id = 0
    
    def generate_predictions(input_path, output_path, do_sample, sampling_topp=None, sampling_temperature=None):
        
        if do_sample:
            cfg.generation.sampling = True
            cfg.generation.sampling_topp = sampling_topp
            cfg.generation.temperature = sampling_temperature
            cfg.generation.beam = 1
        else:
            cfg.generation.sampling = False
            cfg.generation.sampling_topp = -1.0
            cfg.generation.temperature = 1.0
            cfg.generation.beam = 1
        generator = task.build_generator(models, cfg.generation)
        
        inputs = []
        image_files = []
        os.makedirs(output_path, exist_ok=True)
        all_files = glob.glob(os.path.join(input_path, "*"))
        image_files = [f for f in all_files if f.lower().endswith(tuple(['.jpg', '.png', '.jpeg', '.webp']))]
        for image in image_files:
            # inputs.append(f"[image]{image}<tab><grounding>An image of")
            inputs.append(f"[image]{image}<tab><grounding>Describe this image in detail:")

        results = []
        for batch in make_batches(inputs, cfg, task, max_positions, encode_fn):
            bsz = batch.src_tokens.size(0)
            src_tokens = batch.src_tokens
            src_lengths = batch.src_lengths
            img_src_tokens = batch.img_src_tokens
            img_gpt_input_mask = batch.img_gpt_input_mask
            constraints = batch.constraints
            if use_cuda:
                src_tokens = src_tokens.cuda()
                src_lengths = src_lengths.cuda()
                if constraints is not None:
                    constraints = constraints.cuda()

            sample = {
                "net_input": {
                    "src_tokens": src_tokens,
                    "src_lengths": src_lengths,
                    "img_src_tokens": img_src_tokens,
                    "img_gpt_input_mask": img_gpt_input_mask,
                },
            }
            translations = task.inference_step(
                generator, models, sample, constraints=constraints
            )
            list_constraints = [[] for _ in range(bsz)]
            if cfg.generation.constraints:
                list_constraints = [unpack_constraints(c) for c in constraints]
            for i, (id, hypos) in enumerate(zip(batch.ids.tolist(), translations)):
                src_tokens_i = utils.strip_pad(src_tokens[i], tgt_dict.pad())
                constraints = list_constraints[i]
                id_ = start_id + id

                src_str = ""
                if src_dict is not None:
                    src_str = src_dict.string(src_tokens_i, cfg.common_eval.post_process)

                # Process top predictions
                for hypo in hypos[: min(len(hypos), cfg.generation.nbest)]:
                    hypo_tokens, hypo_str, alignment = post_process_prediction(
                        hypo_tokens=hypo["tokens"].int().cpu(),
                        src_str=src_str,
                        alignment=hypo["alignment"],
                        align_dict=align_dict,
                        tgt_dict=tgt_dict,
                        remove_bpe=cfg.common_eval.post_process,
                        extra_symbols_to_ignore=get_symbols_to_strip_from_output(generator),
                    )
                    detok_hypo_str = decode_fn(hypo_str)
                    
                    # show the results on the image
                    response_str = detok_hypo_str.split('</image>')[-1]
                    
                    basename = os.path.basename(image_files[id_]).split('.')[0]
                    with open(os.path.join(output_path, f"{basename}.caption"), 'w') as f:
                        f.write(remove_patterns(response_str))
                    
    
        # return vis_image, str(clean_response_str), str(response_str)
    generate_predictions(image_path, output_path, False)

patterns = [r' Describe this image in detail: ', r'The image features ']
def remove_patterns(text):
    text = re.sub('<.*?>', '', text)
    for pattern in patterns:
        text = re.sub(pattern, '', text)
    return text

def remove_special_fields(text):
    return re.sub('<.*?>', '', text)
  
def find_phrases(text):  
    phrases = re.finditer('<phrase>(.*?)</phrase>', text)  
    return [(match.group(1), match.start(1), match.end(1)) for match in phrases]  
  
def adjust_phrase_positions(phrases, text):  
    positions = []  
    for phrase, start, end in phrases:  
        adjusted_start = len(remove_special_fields(text[:start]))  
        adjusted_end = len(remove_special_fields(text[:end]))  
        positions.append((phrase, adjusted_start, adjusted_end))  
    return positions  
  
def mark_words(text, phrases):  
    marked_words = []  
  
    words = re.findall(r'\b\w+\b|[.,;?!:()"“”‘’\']', text)  
    word_indices = [match.start() for match in re.finditer(r'\b\w+\b|[.,;?!:()"“”‘’\']', text)]  
  
    for i, word in enumerate(words):  
        if any(start <= word_indices[i] < end for _, start, end in phrases):  
            marked_words.append((word, 'box'))  
        else:  
            marked_words.append((word, None))  
  
    return marked_words  

def merge_adjacent_words(marked_words):  
    merged_words = []  
    current_word, current_flag = marked_words[0]  
  
    for word, flag in marked_words[1:]:  
        if flag == current_flag:  
            current_word += " " + word  
        else:  
            merged_words.append((current_word, current_flag))  
            current_word = word  
            current_flag = flag  
  
    merged_words.append((current_word, current_flag))  
    return merged_words

def mark_texts(text):
    cleaned_text = remove_special_fields(text)      
    phrases = find_phrases(text)  
    adjusted_phrases = adjust_phrase_positions(phrases, text)      
    marked_words = mark_words(cleaned_text, adjusted_phrases)  
    merge_words = merge_adjacent_words(marked_words)
    return merge_words

# changed from fairseq.utils.py
def post_process_prediction(
    hypo_tokens,
    src_str,
    alignment,
    align_dict,
    tgt_dict,
    remove_bpe=None,
    extra_symbols_to_ignore=None,
):
    hypo_str = tgt_dict.string(
        hypo_tokens, remove_bpe, extra_symbols_to_ignore=extra_symbols_to_ignore
    )
    if align_dict is not None:
        hypo_str = utils.replace_unk(
            hypo_str, src_str, alignment, align_dict, tgt_dict.unk_string()
        )
    if align_dict is not None or remove_bpe is not None:
        # Convert back to tokens for evaluating with unk replacement or without BPE
        # Note that the dictionary can be modified inside the method.
        hypo_tokens = tgt_dict.encode_line(hypo_str, add_if_not_exist=False)
    return hypo_tokens, hypo_str, alignment

def cli_main():
    parser = options.get_interactive_generation_parser()
    parser.add_argument('--image_path', type=str, default=None, help='path to image')
    parser.add_argument('--output_path', type=str, default=None, help='')
    args = options.parse_args_and_arch(parser)
    distributed_utils.call_main(convert_namespace_to_omegaconf(args), main, 
                                image_path=args.image_path, 
                                output_path=args.output_path)


if __name__ == "__main__":    
    cli_main()