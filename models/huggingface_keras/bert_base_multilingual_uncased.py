# labels: test_group::mlagility name::bert_base_multilingual_uncased author::huggingface_keras
"""
https://huggingface.co/bert-base-multilingual-uncased
"""

from mlagility.parser import parse
import transformers
import tensorflow as tf

tf.random.set_seed(0)

# Parsing command-line arguments
batch_size, max_seq_length = parse(["batch_size", "max_seq_length"])


config = transformers.AutoConfig.from_pretrained("bert-base-multilingual-uncased")
model = transformers.TFAutoModel.from_config(config)
inputs = {
    "input_ids": tf.ones(shape=(batch_size, max_seq_length), dtype=tf.int32),
}

# Call model
model(**inputs)