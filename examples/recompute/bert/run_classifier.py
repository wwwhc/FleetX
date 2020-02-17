#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Finetuning on classification tasks."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import argparse
import numpy as np
import subprocess
import multiprocessing

import paddle
import paddle.fluid as fluid

import reader.cls as reader
from model.bert import BertConfig
from model.classifier import create_model
from optimization import optimization
from utils.args import ArgumentGroup, print_arguments, check_cuda
from utils.init import init_pretraining_params, init_checkpoint
from utils.cards import get_cards
import dist_utils
from paddle.fluid.incubate.fleet.collective import fleet, DistributedStrategy
import paddle.fluid.incubate.fleet.base.role_maker as role_maker


from paddle.fluid.incubate.fleet.collective import fleet, DistributedStrategy
import paddle.fluid.incubate.fleet.base.role_maker as role_maker
from env import dist_env

num_trainers = int(os.environ.get('PADDLE_TRAINERS_NUM', 1))

# yapf: disable
parser = argparse.ArgumentParser(__doc__)
model_g = ArgumentGroup(parser, "model", "model configuration and paths.")
model_g.add_arg("bert_config_path",         str,  None,           "Path to the json file for bert model config.")
model_g.add_arg("init_checkpoint",          str,  None,           "Init checkpoint to resume training from.")
model_g.add_arg("init_pretraining_params",  str,  None,
                "Init pre-training params which preforms fine-tuning from. If the "
                 "arg 'init_checkpoint' has been set, this argument wouldn't be valid.")
model_g.add_arg("checkpoints",              str,  "checkpoints",  "Path to save checkpoints.")

train_g = ArgumentGroup(parser, "training", "training options.")
train_g.add_arg("epoch",             int,    3,       "Number of epoches for fine-tuning.")
train_g.add_arg("learning_rate",     float,  5e-5,    "Learning rate used to train with warmup.")
train_g.add_arg("lr_scheduler",      str,    "linear_warmup_decay",
                "scheduler of learning rate.", choices=['linear_warmup_decay', 'noam_decay'])
train_g.add_arg("weight_decay",      float,  0.01,    "Weight decay rate for L2 regularizer.")
train_g.add_arg("warmup_proportion", float,  0.1,
                "Proportion of training steps to perform linear learning rate warmup for.")
train_g.add_arg("save_steps",        int,    10000,   "The steps interval to save checkpoints.")
train_g.add_arg("validation_steps",  int,    1000,    "The steps interval to evaluate model performance.")
train_g.add_arg("use_fp16",          bool,   False,   "Whether to use fp16 mixed precision training.")
train_g.add_arg("use_recompute",          bool,   True,   "Whether to use recompute optimizer for training.")
train_g.add_arg("loss_scaling",      float,  1.0,
                "Loss scaling factor for mixed precision training, only valid when use_fp16 is enabled.")

log_g = ArgumentGroup(parser,     "logging", "logging related.")
log_g.add_arg("skip_steps",          int,    10,    "The steps interval to print loss.")
log_g.add_arg("verbose",             bool,   False, "Whether to output verbose log.")

data_g = ArgumentGroup(parser, "data", "Data paths, vocab paths and data processing options")
data_g.add_arg("data_dir",      str,  None,  "Path to training data.")
data_g.add_arg("vocab_path",    str,  None,  "Vocabulary path.")
data_g.add_arg("max_seq_len",   int,  512,   "Number of words of the longest seqence.")
data_g.add_arg("batch_size",    int,  32,    "Total examples' number in batch for training. see also --in_tokens.")
data_g.add_arg("in_tokens",     bool, False,
              "If set, the batch size will be the maximum number of tokens in one batch. "
              "Otherwise, it will be the maximum number of examples in one batch.")
data_g.add_arg("do_lower_case", bool, True,
               "Whether to lower case the input text. Should be True for uncased models and False for cased models.")
data_g.add_arg("random_seed",   int,  0,     "Random seed.")
data_g.add_arg("shuffle_seed",   int,  123,     "Shuffle seed.")

run_type_g = ArgumentGroup(parser, "run_type", "running type options.")
run_type_g.add_arg("use_cuda",                     bool,   True,  "If set, use GPU for training.")
run_type_g.add_arg("use_fast_executor",            bool,   False, "If set, use fast parallel executor (in experiment).")
run_type_g.add_arg("shuffle",                      bool,   True,  "")
run_type_g.add_arg("num_iteration_per_drop_scope", int,    1,     "Ihe iteration intervals to clean up temporary variables.")
run_type_g.add_arg("task_name",                    str,    None,
                   "The name of task to perform fine-tuning, should be in {'xnli', 'mnli', 'cola', 'mrpc'}.")
run_type_g.add_arg("do_train",                     bool,   True,  "Whether to perform training.")
run_type_g.add_arg("do_val",                       bool,   True,  "Whether to perform evaluation on dev data set.")
run_type_g.add_arg("do_test",                      bool,   True,  "Whether to perform evaluation on test data set.")

parser.add_argument("--enable_ce", action='store_true', help="The flag indicating whether to run the task for continuous evaluation.")

args = parser.parse_args()
# yapf: enable.


def evaluate(exe, test_program, test_pyreader, fetch_list, eval_phase):
    total_cost, total_acc, total_num_seqs = [], [], []
    time_begin = time.time()
    for data in test_pyreader():
        np_loss, np_acc, np_num_seqs = exe.run(program=test_program,
						   feed=data,
                                                   fetch_list=fetch_list)
        total_cost.extend(np_loss * np_num_seqs)
        total_acc.extend(np_acc * np_num_seqs)
        total_num_seqs.extend(np_num_seqs)
    time_end = time.time()
    print("[%s evaluation] ave loss: %f, ave acc: %f, elapsed time: %f s" %
          (eval_phase, np.sum(total_cost) / np.sum(total_num_seqs),
           np.sum(total_acc) / np.sum(total_num_seqs), time_end - time_begin))

def get_device_num():
    # NOTE(zcd): for multi-processe training, each process use one GPU card.
    if num_trainers > 1 : return 1
    visible_device = os.environ.get('CUDA_VISIBLE_DEVICES', None)
    if visible_device:
        device_num = len(visible_device.split(','))
    else:
        device_num = subprocess.check_output(['nvidia-smi','-L']).decode().count('\n')
    return device_num

def main(args):
    bert_config = BertConfig(args.bert_config_path)
    bert_config.print_config()
 
  
    if args.use_cuda:
        place = fluid.CUDAPlace(int(os.getenv('FLAGS_selected_gpus', '0')))
        dev_count = get_device_num()
    else:
        place = fluid.CPUPlace()
        dev_count = int(os.environ.get('CPU_NUM', multiprocessing.cpu_count()))
   
    if args.do_train:
        # only training program run with fleet
        my_dist_env = dist_env()
        worker_endpoints_env = my_dist_env["trainer_endpoints"]
        worker_endpoints = worker_endpoints_env.split(",")
        current_endpoint = my_dist_env["current_endpoint"]
        trainer_id = worker_endpoints.index(current_endpoint)
        # new rolemaker here
        print("current_id: ", trainer_id)
        print("worker_endpoints: ", worker_endpoints)
        role = role_maker.UserDefinedCollectiveRoleMaker(
        current_id=trainer_id,
        worker_endpoints=worker_endpoints)
        # Fleet get role of each worker
        fleet.init(role) 

    exe = fluid.Executor(place)

    # init program
    train_program = fluid.Program()
    startup_prog = fluid.Program()
    
    if args.random_seed != 0:
        print("set program random seed as: ", args.random_seed)
        startup_prog.random_seed = args.random_seed
        train_program.random_seed = args.random_seed

    task_name = args.task_name.lower()
    processors = {
        'xnli': reader.XnliProcessor,
        'cola': reader.ColaProcessor,
        'mrpc': reader.MrpcProcessor,
        'mnli': reader.MnliProcessor,
    }
    
    print("max_seq_len: ", args.max_seq_len)
    print("random_seed: ", args.random_seed)
    processor = processors[task_name](data_dir=args.data_dir,
                                      vocab_path=args.vocab_path,
                                      max_seq_len=args.max_seq_len,
                                      do_lower_case=args.do_lower_case,
                                      in_tokens=args.in_tokens,
                                      random_seed=args.random_seed)
    num_labels = len(processor.get_labels())

    if not (args.do_train or args.do_val or args.do_test):
        raise ValueError("For args `do_train`, `do_val` and `do_test`, at "
                         "least one of them must be True.")

    if args.do_train:
        dev_count = len(worker_endpoints)
        # we need to keep every trainer of fleet the same shuffle_seed
        print("shuffle_seed: ", args.shuffle_seed)
        train_data_generator = processor.data_generator(
            batch_size=args.batch_size,
            phase='train',
            epoch=args.epoch,
            dev_count=dev_count,
            dev_idx=trainer_id,
            shuffle=args.shuffle,
            shuffle_seed=args.shuffle_seed)

        num_train_examples = processor.get_num_examples(phase='train')

        if args.in_tokens:
            max_train_steps = args.epoch * num_train_examples // (
                args.batch_size // args.max_seq_len) // dev_count
        else:
            max_train_steps = args.epoch * num_train_examples // args.batch_size // dev_count

        warmup_steps = int(max_train_steps * args.warmup_proportion)
        print("Device count: %d" % dev_count)
        print("Num train examples: %d" % num_train_examples)
        print("Max train steps: %d" % max_train_steps)
        print("Num warmup steps: %d" % warmup_steps)

        exec_strategy = fluid.ExecutionStrategy()
        exec_strategy.use_experimental_executor = args.use_fast_executor
        exec_strategy.num_threads = dev_count
        exec_strategy.num_iteration_per_drop_scope = args.num_iteration_per_drop_scope
   
        dist_strategy = DistributedStrategy()
        dist_strategy.exec_strategy = exec_strategy
        dist_strategy.nccl_comm_num = 3
        dist_strategy.use_hierarchical_allreduce = True
        
        if args.use_recompute:
            dist_strategy.forward_recompute = True
            dist_strategy.enable_sequential_execution=True
        #dist_strategy.mode = "collective"
        #dist_strategy.collective_mode = "grad_allreduce"

        with fluid.program_guard(train_program, startup_prog):
            with fluid.unique_name.guard():
                train_pyreader, loss, probs, accuracy, num_seqs, checkpoints = create_model(
                    args,
                    bert_config=bert_config,
                    num_labels=num_labels)
                if args.use_recompute:
                    dist_strategy.recompute_checkpoints=checkpoints
                scheduled_lr = optimization(
                    loss=loss,
                    warmup_steps=warmup_steps,
                    num_train_steps=max_train_steps,
                    learning_rate=args.learning_rate,
                    train_program=train_program,
                    startup_prog=startup_prog,
                    weight_decay=args.weight_decay,
                    scheduler=args.lr_scheduler,
                    use_fp16=args.use_fp16,
                    loss_scaling=args.loss_scaling,
                    dist_strategy = dist_strategy)

        if args.verbose:
            if args.in_tokens:
                lower_mem, upper_mem, unit = fluid.contrib.memory_usage(
                    program=train_program,
                    batch_size=args.batch_size // args.max_seq_len)
            else:
                lower_mem, upper_mem, unit = fluid.contrib.memory_usage(
                    program=train_program, batch_size=args.batch_size)
            print("Theoretical memory usage in training: %.3f - %.3f %s" %
                  (lower_mem, upper_mem, unit))

    if args.do_val:
        dev_prog = fluid.Program()
        with fluid.program_guard(dev_prog, startup_prog):
            with fluid.unique_name.guard():
                dev_pyreader, loss, probs, accuracy, num_seqs, _ = create_model(
                    args,
                    bert_config=bert_config,
                    num_labels=num_labels)

        dev_prog = dev_prog.clone(for_test=True)
        dev_pyreader.decorate_batch_generator(
                            processor.data_generator(
                                batch_size=args.batch_size,
                                phase='dev',
                                epoch=1,
                                dev_count=1,
                                dev_idx=0,
                                shuffle=False), place)

    if args.do_test:
        test_prog = fluid.Program()
        with fluid.program_guard(test_prog, startup_prog):
            with fluid.unique_name.guard():
                test_pyreader, loss, probs, accuracy, num_seqs, _ = create_model(
                    args,
                    bert_config=bert_config,
                    num_labels=num_labels)

        test_prog = test_prog.clone(for_test=True)
        test_pyreader.decorate_batch_generator(
                            processor.data_generator(
                                batch_size=args.batch_size,
                                phase='test',
                                epoch=1,
                                dev_count=1,
                                dev_idx=0,
                                shuffle=False), place)

    exe.run(startup_prog)

    if args.do_train:
        if args.init_checkpoint and args.init_pretraining_params:
            print(
                "WARNING: args 'init_checkpoint' and 'init_pretraining_params' "
                "both are set! Only arg 'init_checkpoint' is made valid.")
        if args.init_checkpoint:
            init_checkpoint(
                exe,
                args.init_checkpoint,
                main_program=startup_prog,
                use_fp16=args.use_fp16)
        elif args.init_pretraining_params:
            init_pretraining_params(
                exe,
                args.init_pretraining_params,
                main_program=startup_prog,
                use_fp16=args.use_fp16)
    elif args.do_val or args.do_test:
        if not args.init_checkpoint:
            raise ValueError("args 'init_checkpoint' should be set if"
                             "only doing validation or testing!")
        init_checkpoint(
            exe,
            args.init_checkpoint,
            main_program=startup_prog,
            use_fp16=args.use_fp16)

    #if args.do_train:
        #build_strategy = fluid.BuildStrategy()

        
        #if args.use_cuda and num_trainers > 1:
       #    assert shuffle_seed is not None
        #    dist_utils.prepare_for_multi_process(exe, build_strategy, train_program)
        #    train_data_generator = fluid.contrib.reader.distributed_batch_reader(
        #          train_data_generator)

        #train_compiled_program = fluid.CompiledProgram(train_program).with_data_parallel(
        #         loss_name=loss.name, build_strategy=build_strategy)

        #train_pyreader.decorate_batch_generator(train_data_generator, place)


    if args.do_train:
        train_pyreader.decorate_batch_generator(train_data_generator, place)
        
        steps = 0
        total_cost, total_acc, total_num_seqs = [], [], []
        time_begin = time.time()
        throughput = []
        ce_info = []
        #while True:
        #    try:

        if True:
            for data in train_pyreader():
                # steps += 1
                if steps % args.skip_steps == 0:
                    if warmup_steps <= 0:
                        fetch_list = [loss.name, accuracy.name, num_seqs.name]
                    else:
                        fetch_list = [
                            loss.name, accuracy.name, scheduled_lr.name,
                            num_seqs.name
                        ]
                else:
                    fetch_list = []
                
                outputs = exe.run(fleet.main_program, feed=data, fetch_list=fetch_list)

                if steps % args.skip_steps == 0:
                    if warmup_steps <= 0:
                        np_loss, np_acc, np_num_seqs = outputs
                    else:
                        np_loss, np_acc, np_lr, np_num_seqs = outputs

                    total_cost.extend(np_loss * np_num_seqs)
                    total_acc.extend(np_acc * np_num_seqs)
                    total_num_seqs.extend(np_num_seqs)

                    if args.verbose:
                        verbose = "train pyreader queue size: %d, " % train_pyreader.queue.size(
                        )
                        verbose += "learning rate: %f" % (
                            np_lr[0]
                            if warmup_steps > 0 else args.learning_rate)
                        print(verbose)

                    current_example, current_epoch = processor.get_train_progress(
                    )
                    time_end = time.time()
                    used_time = time_end - time_begin

                    log_record = "epoch: {}, progress: {}/{}, step: {}, ave loss: {}, ave acc: {}".format(
                           current_epoch, current_example, num_train_examples,
                           steps, np.sum(total_cost) / np.sum(total_num_seqs),
                           np.sum(total_acc) / np.sum(total_num_seqs))
                    ce_info.append([np.sum(total_cost) / np.sum(total_num_seqs), np.sum(total_acc) / np.sum(total_num_seqs), used_time])
                    if steps > 0 :
                        throughput.append( args.skip_steps / used_time)
                        log_record = log_record + ", speed: %f steps/s" % (args.skip_steps / used_time)
                        print(log_record)
                    else:
                        print(log_record)
                    total_cost, total_acc, total_num_seqs = [], [], []
                    time_begin = time.time()

                steps += 1
                if steps % args.save_steps == 0 and trainer_id == 0:
                    save_path = os.path.join(args.checkpoints,
                                             "step_" + str(steps))
                    fluid.io.save_persistables(exe, save_path, train_program)

                if steps % args.validation_steps == 0 and trainer_id == 0:
                    print("Average throughtput: %s" % (np.average(throughput)))
                    throughput = []
                    # evaluate dev set
                    if args.do_val:
                        evaluate(exe, dev_prog, dev_pyreader,
                                 [loss.name, accuracy.name, num_seqs.name],
                                 "dev")
                    # evaluate test set
                    if args.do_test:
                        evaluate(exe, test_prog, test_pyreader,
                                 [loss.name, accuracy.name, num_seqs.name],
                                 "test")
             
        if args.enable_ce:
            card_num = get_cards()
            ce_cost = 0
            ce_acc = 0
            ce_time = 0
            try:
                ce_cost = ce_info[-2][0]
                ce_acc = ce_info[-2][1]
                ce_time = ce_info[-2][2]
            except:
                print("ce info error")
            print("kpis\ttrain_duration_%s_card%s\t%s" %
                (args.task_name, card_num, ce_time))
            print("kpis\ttrain_cost_%s_card%s\t%f" %
                (args.task_name, card_num, ce_cost))
            print("kpis\ttrain_acc_%s_card%s\t%f" %
                (args.task_name, card_num, ce_acc))


    # final eval on dev set
    if args.do_val:
        print("Final validation result:")
        evaluate(exe, dev_prog, dev_pyreader,
                 [loss.name, accuracy.name, num_seqs.name], "dev")

    print("Final test result:")
    print("Empty")

    # final eval on test set
    if args.do_test:
        print("Final test result:")
        evaluate(exe, test_prog, test_pyreader,
                 [loss.name, accuracy.name, num_seqs.name], "test")


if __name__ == '__main__':
    print_arguments(args)
    check_cuda(args.use_cuda)
    main(args)
