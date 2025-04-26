import logging
import grpc
import os
import time
import json
import threading
import uuid
from datetime import datetime

from grpc_comm import inference_pb2_grpc, inference_pb2, grpc_client
from inference.model_loader import load_model
from inference.speculative import speculative_decode
from transformers import AutoTokenizer
import torch

logger = logging.getLogger(__name__)


def save_perf_stats(perf_stats: dict, file_prefix: str):
    """
    Save perf_stats to <file_prefix>.csv (append a row) and
    <file_prefix>.json (overwrite latest snapshot).
    """
    csv_path  = f"{file_prefix}.csv"
    json_path = f"{file_prefix}.json"
    try:
        total_time_val = perf_stats.get("total_time", 0.0)
        
        def fmt(t):
            if total_time_val > 0:
                pct = (t / total_time_val) * 100.0
                return f"{pct:.1f}%({t:.3f})"
            else:
                return f"0.0%({t:.3f})"
        
        # Append CSV row; write header if file does not exist
        header = ["total_time", "tokens_generated", "tokens_per_second",
                  "avg_token_time", "token_match_rate",
                  "draft_forward_time", "grpc_server_time",
                  "target_verification_time", "rollback_time"]

        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline='') as cf:
            if write_header:
                cf.write(",".join(header) + "\n")
            row = [
                f"{total_time_val:.3f}",
                perf_stats.get("tokens_generated", ""),
                perf_stats.get("throughput", ""),
                perf_stats.get("avg_token_time", ""),
                perf_stats.get("token_match_rate", ""),
                fmt(perf_stats.get("draft_forward_time", 0.0)),
                fmt(perf_stats.get("grpc_server_time", 0.0)),
                fmt(perf_stats.get("target_verification_time", 0.0)),
                fmt(perf_stats.get("rollback_time", 0.0)),
            ]
            cf.write(",".join(str(x) for x in row) + "\n")

        # Always dump latest JSON snapshot
        with open(json_path, "w") as jf:
            json.dump(perf_stats, jf, indent=2)
        logger.info(f"Perf metrics appended to {csv_path} (snapshot {json_path})")
    except Exception as e:
        logger.error(f"Failed to save performance data: {e}")


def run_prompt_file(
    draft_model_name: str,
    target_host: str = "localhost",
    port: int = 50051,
    prompt_text_file: str = "",
    target_tokenizer: str = None,
    max_new_tokens: int = 50,
    sequence_length: int = 128,
    gamma: int = 4,
    profile: bool = False,
    top_p: float = 0.9,
    temperature: float = 1.0
):
    if not os.path.exists(prompt_text_file):
        logger.error(f"Prompt text file not found: {prompt_text_file}")
        return
    with open(prompt_text_file, 'r', encoding='utf-8') as f:
        prompts = [line.strip() for line in f if line.strip()]
    if not prompts:
        logger.error("No valid lines in the prompt file.")
        return

    logger.info(f"Loading draft model '{draft_model_name}' (sequence_length={sequence_length}) for speculative decoding...")
    if isinstance(draft_model_name, str):
        # draft_model_name is a path → load the model
        draft_model = load_model(
            draft_model_name,
            sequence_length=sequence_length,
            spec_length=gamma
        )
        model_path_str = draft_model_name
    else:
        TypeError("draft_model_name must be a string (path).")
        # never happens in Neuron
        # already a model instance
        draft_model = draft_model_name
        # try to recover a path for tokenizer fallback
        model_path_str = getattr(getattr(draft_model, "config", None), "_name_or_path", None)

    # Decide which tokenizer to load
    if target_tokenizer:
        tokenizer_source = target_tokenizer
    elif isinstance(model_path_str, str):
        tokenizer_source = model_path_str
    else:
        raise ValueError(
            "Cannot determine tokenizer_source: provide --target_tokenizer when "
            "passing a pre‑loaded draft model."
        )
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_source, use_fast=False)
    
    address = f"{target_host}:{port}"
    channel = grpc.insecure_channel(address)
    stub = inference_pb2_grpc.SpeculativeServiceStub(channel)

    # We'll create a single session_id for each prompt, or we can unify them.
    # For now, let's do one session per prompt, but handle them in a single pass.

    # Step 1) StartGeneration for each prompt
    session_ids = [] # list of session IDs
    for prompt in prompts: # support multiple prompts
        logger.info(f"Starting prefilling for prompt: '{prompt}'")
        sid = _gen_session_id()
        session_ids.append(sid)
        stub.StartGeneration(
            inference_pb2.StartRequest(
                session_id=sid,
                prompt=prompt,
                max_new_tokens=max_new_tokens,
                gamma=gamma
            )
        )

    final_texts = [prompts[i] for i in range(len(prompts))]
    finished_mask = [False]*len(prompts)
    tokens_generated = [0]*len(prompts)
    accepted_counts = [0]*len(prompts)
    target_counts = [0]*len(prompts)

    # do up to max_new_tokens steps in batch
    import time
    start_time = time.time()

    # a loop in Python that calls speculative_decode for each prompt in sequence
    for i, prompt in enumerate(prompts):
        logger.info(f"Decoding prompt {i}: {prompt}")
        gen_text, perf_stats = speculative_decode(
            draft_model, tokenizer, stub,
            prompt, max_new_tokens, gamma,
            profile=profile, top_p=top_p, temperature=temperature,
            session_id=session_ids[i]
        )
        final_texts[i] = prompt + gen_text
        if perf_stats:
            accepted_counts[i] = perf_stats.get("accepted_tokens_total", 0)
            target_counts[i] = perf_stats.get("target_tokens_total", 0)
            if profile:
                # Record prompt index so rows can be distinguished, but
                # append all rows to a single CSV file.
                perf_stats["prompt_id"] = i
                save_perf_stats(perf_stats, file_prefix="performance_speculative")

    end_time = time.time()
    total_time = end_time - start_time
    logger.info(f"Batched decode completed in {total_time:.2f}s.")

    print("\n=== Final Outputs (BATCH approach) ===")
    for i, text in enumerate(final_texts):
        print(f"[Prompt {i} Output]:\n{text}\n")


def run_client(draft_model_name: str,
               target_host: str = "localhost",
               port: int = 50051,
               prompt: str = "",
               target_tokenizer: str = None,
               max_new_tokens: int = 50,
               sequence_length: int = 128,
               gamma: int = 4,
               profile: bool = False,
               no_target: bool = False,
               top_p: float = 0.9,
               temperature: float = 1.0):
    # same as existing
    logger.info(f"Loading draft model '{draft_model_name}' (sequence_length={sequence_length})...")
    draft_model = load_model(
        draft_model_name,
        sequence_length=sequence_length,
        spec_length=gamma
    )
    tokenizer_source = target_tokenizer or draft_model_name
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_source, use_fast=False)
    if not prompt:
        logger.error("No prompt provided.")
        return
    if no_target:
        return _run_standalone_draft(draft_model, tokenizer, prompt, max_new_tokens, profile)
    else:
        address = f"{target_host}:{port}"
        logger.info(f"Connecting to target server at {address}...")
        channel = grpc.insecure_channel(address)
        stub = inference_pb2_grpc.SpeculativeServiceStub(channel)
        session_id = _gen_session_id()
        stub.StartGeneration(
            inference_pb2.StartRequest(
                session_id=session_id,
                prompt=prompt,
                max_new_tokens=max_new_tokens,
                gamma=gamma
            )
        )
        logger.info(f"Starting speculative decoding (single) for prompt: '{prompt}'")
        generated_text, perf_stats = speculative_decode(
            draft_model, tokenizer, stub, prompt, max_new_tokens, gamma,
            profile=profile, top_p=top_p, temperature=temperature,
            session_id=session_id
        )
        full_output = prompt + generated_text
        print("\n=== Final Output ===\n" + full_output)
        if profile and perf_stats:
            save_perf_stats(perf_stats, file_prefix="performance_speculative")
        return full_output


def _run_standalone_draft(draft_model, tokenizer, prompt, max_new_tokens, profile):
    # same as existing
    output_text = ""
    input_ids = tokenizer(prompt, return_tensors='pt').input_ids
    tokens_generated = 0
    start_time = time.time() if profile else None
    for i in range(max_new_tokens):
        try:
            output = draft_model.sample(input_ids, sequence_length=input_ids.shape[1] + 1)
        except Exception as e:
            logger.error(f"Draft model generation failed: {e}")
            break
        token_id = int(output[0, -1]) if not isinstance(output, (list, tuple)) else int(output[0][-1])
        token_text = tokenizer.decode([token_id], clean_up_tokenization_spaces=False)
        output_text += token_text
        new_token_tensor = torch.tensor([[token_id]], dtype=input_ids.dtype)
        input_ids = torch.cat([input_ids, new_token_tensor], dim=1)
        tokens_generated += 1
        if tokenizer.eos_token_id is not None and token_id == tokenizer.eos_token_id:
            break
    end_time = time.time() if profile else None
    if profile and start_time is not None:
        total_time = end_time - start_time
        throughput = tokens_generated / total_time if total_time > 0 else float('inf')
        logger.info(f"Draft model generation completed in {total_time:.2f} seconds. Throughput={throughput:.2f} t/s")
    full_output = prompt + output_text
    print("\n=== Final Output ===\n" + full_output)
    return full_output


def run_concurrent_clients(draft_model_name: str,
                           target_host: str = "localhost",
                           port: int = 50051,
                           prompt_text_file: str = "",
                           target_tokenizer: str = None,
                           max_new_tokens: int = 50,
                           sequence_length: int = 128,
                           gamma: int = 4,
                           profile: bool = False,
                           no_target: bool = False,
                           top_p: float = 0.9,
                           temperature: float = 1.0):
    # same as existing concurrency approach
    if not os.path.exists(prompt_text_file):
        logger.error(f"Prompt text file not found: {prompt_text_file}")
        return
    with open(prompt_text_file, 'r', encoding='utf-8') as f:
        prompts = [line.strip() for line in f if line.strip()]
    if not prompts:
        logger.error("No valid lines in prompt file.")
        return
    logger.info(f"Loading draft model '{draft_model_name}' (sequence_length={sequence_length}) for concurrency...")
    draft_model = load_model(
        draft_model_name,
        sequence_length=sequence_length,
        spec_length=gamma
    )
    tokenizer_source = target_tokenizer or draft_model_name
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_source, use_fast=False)
    address = f"{target_host}:{port}"
    channel = None
    stub = None
    if not no_target:
        logger.info(f"Connecting to target server at {address} for concurrency...")
        channel = grpc.insecure_channel(address)
        stub = inference_pb2_grpc.SpeculativeServiceStub(channel)
    results = [None]*len(prompts)
    threads = []

    def worker(idx, prompt_text):
        if no_target:
            out = _run_standalone_draft(draft_model, tokenizer, prompt_text, max_new_tokens, profile)
            results[idx] = out
        else:
            session_id = _gen_session_id()
            stub.StartGeneration(
                inference_pb2.StartRequest(
                    session_id=session_id,
                    prompt=prompt_text,
                    max_new_tokens=max_new_tokens,
                    gamma=gamma
                )
            )
            logger.info(f"[Thread-{idx}] Starting speculative decoding with session_id={session_id}")
            gen_text, perf_stats = speculative_decode(
                draft_model, tokenizer, stub,
                prompt_text, max_new_tokens, gamma,
                profile=profile, top_p=top_p, temperature=temperature,
                session_id=session_id
            )
            full_output = prompt_text + gen_text
            results[idx] = full_output
            if profile and perf_stats:
                prefix = f"performance_speculative_prompt{idx}"
                save_perf_stats(perf_stats, file_prefix=prefix)

    for i, prompt_text in enumerate(prompts):
        t = threading.Thread(target=worker, args=(i, prompt_text), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    print("\n=== Final Batched Outputs ===")
    for i, text in enumerate(results):
        print(f"\n[Prompt {i} Output]:\n{text}")


def _gen_session_id():
    return int(uuid.uuid4()) & 0xFFFFFFFF