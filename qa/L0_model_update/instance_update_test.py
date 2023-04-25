# Copyright 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import unittest
import random
import time
import concurrent.futures
import numpy as np
import tritonclient.grpc as grpcclient
from tritonclient.utils import InferenceServerException
from models.model_init_del.util import (get_count, reset_count, set_delay,
                                        update_instance_group,
                                        update_model_file, enable_batching,
                                        disable_batching)


class TestInstanceUpdate(unittest.TestCase):

    __model_name = "model_init_del"

    def setUp(self):
        # Reset counters
        reset_count("initialize")
        reset_count("finalize")
        # Reset batching
        disable_batching()
        # Reset delays
        set_delay("initialize", 0)
        set_delay("infer", 0)
        # Initialize client
        self.__triton = grpcclient.InferenceServerClient("localhost:8001",
                                                         verbose=True)

    def __get_inputs(self, batching=False):
        self.assertIsInstance(batching, bool)
        if batching:
            shape = [random.randint(1, 2), random.randint(1, 16)]
        else:
            shape = [random.randint(1, 16)]
        inputs = [grpcclient.InferInput("INPUT0", shape, "FP32")]
        inputs[0].set_data_from_numpy(np.ones(shape, dtype=np.float32))
        return inputs

    def __poll_finalize_count(self, expected_finalize_count):
        timeout = 30  # seconds
        poll_interval = 0.1  # seconds
        max_retry = timeout / poll_interval
        num_retry = 0
        while (num_retry < max_retry and
               get_count("finalize") < expected_finalize_count):
            time.sleep(poll_interval)
            num_retry += 1

    def __load_model(self, instance_count, instance_config=""):
        self.__update_instance_count(instance_count, 0, instance_config)

    def __update_instance_count(self,
                                add_count,
                                del_count,
                                instance_config="",
                                model_will_reload=False,
                                batching=False):
        self.assertIsInstance(add_count, int)
        self.assertGreaterEqual(add_count, 0)
        self.assertIsInstance(del_count, int)
        self.assertGreaterEqual(del_count, 0)
        self.assertIsInstance(instance_config, str)
        self.assertIsInstance(model_will_reload, bool)
        prev_initialize_count = get_count("initialize")
        prev_finalize_count = get_count("finalize")
        new_initialize_count = prev_initialize_count + add_count
        new_finalize_count = prev_finalize_count + del_count
        if len(instance_config) == 0:
            prev_count = prev_initialize_count - prev_finalize_count
            new_count = prev_count + add_count - del_count
            instance_config = ("{\ncount: " + str(new_count) +
                               "\nkind: KIND_CPU\n}")
        update_instance_group(instance_config)
        self.__triton.load_model(self.__model_name)
        self.assertEqual(get_count("initialize"), new_initialize_count)
        if model_will_reload:
            self.__poll_finalize_count(new_finalize_count)
        self.assertEqual(get_count("finalize"), new_finalize_count)
        self.__triton.infer(self.__model_name, self.__get_inputs(batching))

    def __unload_model(self, batching=False):
        prev_initialize_count = get_count("initialize")
        prev_finalize_count = get_count("finalize")
        self.__triton.unload_model(self.__model_name)
        self.__poll_finalize_count(prev_initialize_count)
        self.assertEqual(get_count("initialize"), prev_initialize_count)
        self.assertEqual(get_count("finalize"), prev_initialize_count)
        with self.assertRaises(InferenceServerException):
            self.__triton.infer(self.__model_name, self.__get_inputs(batching))

    # Test add -> remove -> add an instance
    def test_add_rm_add_instance(self):
        self.__load_model(3)
        self.__update_instance_count(1, 0)  # add 1 instance
        self.__update_instance_count(0, 1)  # remove 1 instance
        self.__update_instance_count(1, 0)  # add 1 instance
        self.__unload_model()

    # Test remove -> add -> remove an instance
    def test_rm_add_rm_instance(self):
        self.__load_model(2)
        self.__update_instance_count(0, 1)  # remove 1 instance
        self.__update_instance_count(1, 0)  # add 1 instance
        self.__update_instance_count(0, 1)  # remove 1 instance
        self.__unload_model()

    # Test add/remove multiple CPU instances at a time
    def test_cpu_instance_update(self):
        self.__load_model(8)
        self.__update_instance_count(0, 4)  # remove 4 instances
        self.__update_instance_count(0, 3)  # remove 3 instances
        self.__update_instance_count(0, 0)  # no change
        self.__update_instance_count(2, 0)  # add 2 instances
        self.__update_instance_count(5, 0)  # add 5 instances
        self.__unload_model()

    # Test add/remove multiple GPU instances at a time
    def test_gpu_instance_update(self):
        self.__load_model(6, "{\ncount: 6\nkind: KIND_GPU\n}")
        self.__update_instance_count(0, 2, "{\ncount: 4\nkind: KIND_GPU\n}")
        self.__update_instance_count(3, 0, "{\ncount: 7\nkind: KIND_GPU\n}")
        self.__unload_model()

    # Test add/remove multiple CPU/GPU instances at a time
    def test_gpu_cpu_instance_update(self):
        # Load model with 1 GPU instance and 2 CPU instance
        self.__load_model(
            3,
            "{\ncount: 2\nkind: KIND_CPU\n},\n{\ncount: 1\nkind: KIND_GPU\n}")
        # Add 2 GPU instance and remove 1 CPU instance
        self.__update_instance_count(
            2, 1,
            "{\ncount: 1\nkind: KIND_CPU\n},\n{\ncount: 3\nkind: KIND_GPU\n}")
        # Shuffle the instances
        self.__update_instance_count(
            0, 0,
            "{\ncount: 3\nkind: KIND_GPU\n},\n{\ncount: 1\nkind: KIND_CPU\n}")
        # Remove 1 GPU instance and add 1 CPU instance
        self.__update_instance_count(
            1, 1,
            "{\ncount: 2\nkind: KIND_GPU\n},\n{\ncount: 2\nkind: KIND_CPU\n}")
        # Unload model
        self.__unload_model()

    # Test instance update with invalid instance group config
    def test_invalid_config(self):
        # Load model with 8 instances
        self.__load_model(8)
        # Set invalid config
        update_instance_group("--- invalid config ---")
        with self.assertRaises(InferenceServerException):
            self.__triton.load_model("model_init_del")
        # Correct config by reducing instances to 4
        self.__update_instance_count(0, 4)
        # Unload model
        self.__unload_model()

    # Test instance update with model file changed
    def test_model_file_update(self):
        self.__load_model(5)
        update_model_file()
        self.__update_instance_count(6,
                                     5,
                                     "{\ncount: 6\nkind: KIND_CPU\n}",
                                     model_will_reload=True)
        self.__unload_model()

    # Test instance update with non instance config changed in config.pbtxt
    def test_non_instance_config_update(self):
        self.__load_model(4)
        enable_batching()
        self.__update_instance_count(2,
                                     4,
                                     "{\ncount: 2\nkind: KIND_CPU\n}",
                                     model_will_reload=True,
                                     batching=True)
        self.__unload_model(batching=True)

    # Test instance update with an ongoing inference
    def test_update_while_inferencing(self):
        # Load model with 1 instance
        self.__load_model(1)
        # Add 1 instance while inferencing
        set_delay("infer", 10)
        update_instance_group("{\ncount: 2\nkind: KIND_CPU\n}")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            infer_start_time = time.time()
            infer_thread = pool.submit(self.__triton.infer, self.__model_name,
                                       self.__get_inputs())
            time.sleep(2)  # make sure inference has started
            update_start_time = time.time()
            update_thread = pool.submit(self.__triton.load_model,
                                        self.__model_name)
            update_thread.result()
            update_end_time = time.time()
            infer_thread.result()
            infer_end_time = time.time()
        infer_time = infer_end_time - infer_start_time
        update_time = update_end_time - update_start_time
        # Adding a new instance does not depend on existing instances, so the
        # ongoing inference should not block the update.
        self.assertGreaterEqual(infer_time, 10.0, "Invalid infer time")
        self.assertLess(update_time, 5.0, "Update blocked by infer")
        self.assertEqual(get_count("initialize"), 2)
        self.assertEqual(get_count("finalize"), 0)
        self.__triton.infer(self.__model_name, self.__get_inputs())
        # Unload model
        self.__unload_model()

    # Test inference with an ongoing instance update
    def test_infer_while_updating(self):
        # Load model with 1 instance
        self.__load_model(1)
        # Infer while adding 1 instance
        set_delay("initialize", 10)
        update_instance_group("{\ncount: 2\nkind: KIND_CPU\n}")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            update_start_time = time.time()
            update_thread = pool.submit(self.__triton.load_model,
                                        self.__model_name)
            time.sleep(2)  # make sure update has started
            infer_start_time = time.time()
            infer_thread = pool.submit(self.__triton.infer, self.__model_name,
                                       self.__get_inputs())
            infer_thread.result()
            infer_end_time = time.time()
            update_thread.result()
            update_end_time = time.time()
        update_time = update_end_time - update_start_time
        infer_time = infer_end_time - infer_start_time
        # Waiting on new instance creation should not block inference on
        # existing instances.
        self.assertGreaterEqual(update_time, 10.0, "Invalid update time")
        self.assertLess(infer_time, 5.0, "Infer blocked by update")
        self.assertEqual(get_count("initialize"), 2)
        self.assertEqual(get_count("finalize"), 0)
        self.__triton.infer(self.__model_name, self.__get_inputs())
        # Unload model
        self.__unload_model()


if __name__ == "__main__":
    unittest.main()
