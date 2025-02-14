import os
from typing import Dict
import argparse
import timeit
import logging
# from common.fast_inference import FastInferenceInterface
# from common.together_web3.computer import RequestTypeLanguageModelInference
# from common.together_web3.together import TogetherWeb3, TogetherClientOptions
# from utils.fast_inference import FastInferenceInterface
from together_worker.fast_inference import FastInferenceInterface
from together_web3.computer import RequestTypeLanguageModelInference
from together_web3.together import TogetherWeb3, TogetherClientOptions
import torch
import torch.distributed as dist
from torch.nn.utils.rnn import pad_sequence
from utils.gpt import GPT
from utils.para_utils import *
from transformers import AutoTokenizer, AutoConfig
import logging

logger = logging.getLogger(__name__)
logger.setLevel(int(os.environ.get('LOG_LEVEL', logging.DEBUG)))

class FastOPTInference(FastInferenceInterface):
    def __init__(self, model_name: str, args=None) -> None:    
        super().__init__(model_name, args if args is not None else {})
        logging.debug("\n=============== Arguments ===============")
        logging.debug(args.keys())
        logging.debug(args)
        #for key in args.keys():
        #    logging.debug("{}: {}".format(arg, getattr(args, arg)))
        logging.debug("=========================================\n")
        self.tensor_para_size = 1
        self.pipeline_para_size = 1
        self.max_batch_size = args['max_batch_size']
        self.random_seed_tensor = torch.zeros([self.max_batch_size], dtype=torch.int64)
        self.task_info={
            "prompt_seqs": None,
            "output_len":16,
            "beam_width": 1,
            "top_k": 50,
            "top_p": 0,
            "beam_search_diversity_rate": 0,
            "temperature": 0.1,
            "len_penalty": 0,
            "repetition_penalty": 1.0,
            "return_cum_log_probs": 0,
            "return_output_length":0,
        }
        
        hf_config = vars(AutoConfig.from_pretrained(args['hf_model_name']))
        head_num = hf_config['num_attention_heads']
        layer_num = hf_config['num_hidden_layers']
        start_id = hf_config['bos_token_id']
        self.end_id = hf_config['eos_token_id']
        size_per_head = hf_config['hidden_size'] // head_num
        vocab_size = 50272
        max_seq_len = 2048
        layernorm_eps = 1e-5
        layernorm_type = 'pre_layernorm' if hf_config['do_layer_norm_before'] else 'post_layernorm'
        activation_type = 'Relu' if hf_config['activation_function'] == 'relu' else 'Gelu'
        has_post_decoder_layernorm = layernorm_type == 'pre_layernorm'
        lib_path = '/workspace/Port_FasterTransformer/build/lib/libth_gpt.so'
        ckpt_path = args['ckpt_path']
        assert(ckpt_path.endswith("-tp1"))
        self.tokenizer = AutoTokenizer.from_pretrained(args['hf_model_name'], use_fast=False)
        self.tokenizer.pad_token = self.tokenizer.eos_token
        torch.manual_seed(0)
        with torch.no_grad():
            # Prepare model.
            self.opt_model = GPT(head_num, size_per_head, vocab_size, start_id, self.end_id, layer_num,
                                         max_seq_len, self.tensor_para_size, self.pipeline_para_size, lib_path,
                                         layernorm_eps, layernorm_type, activation_type, has_post_decoder_layernorm,
                                         int8_mode=0, weights_data_type='fp16')
            if not self.opt_model.load_w_type(ckpt_path=ckpt_path, infer_data_type='fp16'):
                logging.debug("[WARNING] Checkpoint file not found. Model loading is skipped.")      
        logging.debug(f"<FastOPTInference.__init__> initialization done")
    
    def dispatch_request(self, args, env) -> Dict:
        logging.debug(f"dispatch_request get {args}")
        args = args[0]
        args = {k: v for k, v in args.items() if v is not None}
        # Inputs
        self.task_info["prompt_seqs"] = [args['prompt']]
        self.task_info["output_len"] = get_int(args.get("max_tokens", 16), default=16)
        self.task_info["beam_width"] = get_int(args.get("beam_width", 1), default=1)
        self.task_info["top_k"] = get_int(args.get("top_k", 50), default=50)
        self.task_info["top_p"] = get_float(args.get("top_p", 0.0), default=0.0)
        self.task_info["beam_search_diversity_rate"] = get_float(args.get("beam_search_diversity_rate", 0.0), default=0.0)
        self.task_info["temperature"] = get_float(args.get("temperature", 0.8), default=0.1)
        self.task_info["len_penalty"] = get_float(args.get("len_penalty", 0.0), default=0.0)
        self.task_info["repetition_penalty"] = get_float(args.get("repetition_penalty", 1.0), default=1.0)
        self.task_info["stop"] = args.get("stop", [])
        self.task_info["stream_tokens"] = args.get("stream_tokens", False)
        self.task_info["return_cum_log_probs"] = args.get("return_cum_log_probs", 0)
        self.task_info["return_output_length"] = args.get("return_output_length", 0)
          
        result = self._run_inference()
        logging.debug(f"<FastOPTInference.dispatch_request> return: {result}")
        return result

    def _run_inference(self):
        logging.debug(f"<FastOPTInference._run_inference> enter rank-<{dist.get_rank()}>")
        
        with torch.no_grad():
            contexts = self.task_info["prompt_seqs"]
            start_ids = [torch.IntTensor(self.tokenizer.encode(c)) for c in contexts]
            start_lengths = [len(ids) for ids in start_ids]
            
            start_ids = pad_sequence(start_ids, batch_first=True, padding_value=self.end_id)
            start_lengths = torch.IntTensor(start_lengths)
            logging.debug(f"start_ids: length ({start_ids.shape[0]}) ids: {start_ids}")
            
            time = timeit.default_timer()
            max_batch_size = self.max_batch_size
            tokens_batch = self.opt_model(start_ids,
                                    start_lengths,
                                    self.task_info["output_len"],
                                    self.task_info["beam_width"],
                                    self.task_info["top_k"] * torch.ones(size=[max_batch_size], dtype=torch.int32),
                                    self.task_info["top_p"] * torch.ones(size=[max_batch_size], dtype=torch.float32),
                                    self.task_info["beam_search_diversity_rate"] * torch.ones(size=[max_batch_size], dtype=torch.float32),
                                    self.task_info["temperature"] * torch.ones(size=[max_batch_size], dtype=torch.float32),
                                    self.task_info["len_penalty"] * torch.ones(size=[max_batch_size], dtype=torch.float32),
                                    self.task_info["repetition_penalty"] * torch.ones(size=[max_batch_size], dtype=torch.float32),
                                    self.random_seed_tensor,
                                    self.task_info["return_output_length"],
                                    self.task_info["return_cum_log_probs"])
            # only a thread (rank 0) gets the output, while the others are supposed to return None.
            time_elapsed = timeit.default_timer() - time
        logging.debug("[INFO] OPT time costs: {:.2f} ms. <rank-{}>".format(time_elapsed * 1000, dist.get_rank()))
        
        assert tokens_batch is not None
    
        if self.task_info["return_cum_log_probs"] > 0:
            tokens_batch, _, cum_log_probs = tokens_batch
            logging.debug('[INFO] Log probs of sentences:', cum_log_probs)

        inferenece_result = []
        tokens_batch = tokens_batch.cpu().numpy()
        
        for i, (context, tokens) in enumerate(zip(self.task_info["prompt_seqs"], tokens_batch)):
            item = {'choices': [], }
            for beam_id in range(self.task_info["beam_width"]):
                token = tokens[beam_id][start_lengths[i]:]  # exclude context input from the output
                output = self.tokenizer.decode(token)
                logging.debug(f"[INFO] batch {i}, beam {beam_id}: \n[Context]\n{context}\n\n[Output]\n{output}\n")
                choice = {
                    "text": post_processing_text(output, self.task_info["stop"]),
                    "index": beam_id,
                    "finish_reason": "length"
                }
            item['choices'].append(choice)
            inferenece_result.append(item)
        #  So far coordinator does not support batch. 
        return {
            "result_type": RequestTypeLanguageModelInference,
            "choices": inferenece_result[0]['choices'],
            "raw_compute_time": time_elapsed
        }
        

if __name__ == "__main__":
    
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--together_model_name', type=str, default=os.environ.get('SERVICE', 'Together-opt-1.3b'),
                        help='worker name for together coordinator.')
    parser.add_argument('--hf_model_name', type=str, default='facebook/opt-1.3b',
                        help='hugging face model name (used to load config).')
    parser.add_argument('--ckpt_path', type=str, default='/workspace/Port_FasterTransformer/build/model/opt-1.3b-tp1',
                        help='path to the checkpoint file.')
    parser.add_argument('--worker_name', type=str, default=os.environ.get('WORKER', 'worker1'),
                        help='worker name for together coordinator.')
    parser.add_argument('--group_name', type=str, default=os.environ.get('GROUP', 'group1'),
                        help='group name for together coordinator.')
    
    args = parser.parse_args()
    
    coord_url = os.environ.get("COORD_URL", "127.0.0.1")
    coord_http_port = os.environ.get("COORD_HTTP_PORT", "8092")
    coord_ws_port = os.environ.get("COORD_WS_PORT", "8093")

    coordinator = TogetherWeb3(
        TogetherClientOptions(reconnect=True),
        http_url=f"http://{coord_url}:{coord_http_port}",
        websocket_url=f"ws://{coord_url}:{coord_ws_port}/websocket"
    )
    fip = FastOPTInference(model_name=args.together_model_name, args={
        "coordinator": coordinator,
        "hf_model_name": args.hf_model_name,
        "worker_name": args.worker_name,
        "group_name": args.group_name,
        "ckpt_path": args.ckpt_path,
        "stream_tokens_pipe": False,
        "max_batch_size":1
    })
    fip.start()
