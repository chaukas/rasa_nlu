from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging
import os

import typing
from future.utils import PY3
from typing import List, Text, Any, Optional, Dict

from rasa_nlu.classifiers import INTENT_RANKING_LENGTH
from rasa_nlu.components import Component
from rasa_nlu.config import RasaNLUModelConfig
from rasa_nlu.model import Metadata
from rasa_nlu.training_data import Message
from rasa_nlu.training_data import TrainingData
import numpy as np

try:
    import cPickle as pickle
except:
    import pickle

logger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:
    import tensorflow as tf

try:
    import tensorflow as tf
except ImportError:
    logger.debug('Unable to import tensorflow. '
                 'If you are not using the tensorflow pipeline, '
                 'you can safely ignore this message. '
                 'If you are using pipeline: "tensorflow_embedding", '
                 'this will create an error.')


class EmbeddingIntentClassifier(Component):
    """Intent classifier using supervised embeddings.

    The embedding intent classifier embeds user inputs
    and intent labels into the same space.
    Supervised embeddings are trained by maximizing similarity between them.
    It also provides rankings of the labels that did not "win".

    The embedding intent classifier needs to be preceded by
    a featurizer in the pipeline.
    This featurizer creates the features used for the embeddings.
    It is recommended to use ``intent_featurizer_count_vectors`` that
    can be optionally preceded by ``nlp_spacy`` and ``tokenizer_spacy``.

    Based on the starspace idea from: https://arxiv.org/abs/1709.03856.
    However, in this implementation the `mu` parameter is treated differently
    and additional hidden layers are added together with dropout."""

    name = "intent_classifier_tensorflow_embedding"

    provides = ["intent", "intent_ranking"]

    requires = ["text_features"]

    defaults = {
        # nn architecture
        "num_hidden_layers_a": 2,
        "hidden_layer_size_a": [256, 128],
        "num_hidden_layers_b": 0,
        "hidden_layer_size_b": [],
        "batch_size": 32,
        "epochs": 300,

        # embedding parameters
        "embed_dim": 10,
        "mu_pos": 0.8,  # should be 0.0 < ... < 1.0 for 'cosine'
        "mu_neg": -0.4,  # should be -1.0 < ... < 1.0 for 'cosine'
        "similarity_type": 'cosine',  # string 'cosine' or 'inner'
        "num_neg": 10,
        "use_max_sim_neg": True,  # flag which loss function to use

        # regularization
        "C2": 0.002,
        "C_emb": 0.8,
        "droprate": 0.2,

        # flag if tokenize intents
        "intent_tokenization_flag": False,
        "intent_split_symbol": '_'
    }

    def _load_nn_architecture_params(self):
        self.num_hidden_layers_a = self.component_config['num_hidden_layers_a']
        self.hidden_layer_size_a = self.component_config['hidden_layer_size_a']
        self.num_hidden_layers_b = self.component_config['num_hidden_layers_b']
        self.hidden_layer_size_b = self.component_config['hidden_layer_size_b']
        self.batch_size = self.component_config['batch_size']
        self.epochs = self.component_config['epochs']

    def _load_embedding_params(self):
        self.embed_dim = self.component_config['embed_dim']
        self.mu_pos = self.component_config['mu_pos']
        self.mu_neg = self.component_config['mu_neg']
        self.similarity_type = self.component_config['similarity_type']
        self.num_neg = self.component_config['num_neg']
        self.use_max_sim_neg = self.component_config['use_max_sim_neg']

    def _load_regularization_params(self):
        self.C2 = self.component_config['C2']
        self.C_emb = self.component_config['C_emb']
        self.droprate = self.component_config['droprate']

    def _load_flag_if_tokenize_intents(self):
        self.intent_tokenization_flag = self.component_config[
                                            'intent_tokenization_flag']
        self.intent_split_symbol = self.component_config[
                                            'intent_split_symbol']
        if self.intent_tokenization_flag and not self.intent_split_symbol:
            logger.warning("intent_split_symbol was not specified, "
                           "so intent tokenization will be ignored")

    @staticmethod
    def _check_hidden_layer_sizes(num_layers, layer_size, name=''):
        num_layers = int(num_layers)

        if num_layers < 0:
            logger.error("num_hidden_layers_{} = {} < 0."
                         "Set it to 0".format(name, num_layers))
            num_layers = 0

        if isinstance(layer_size, list) and len(layer_size) != num_layers:
            if len(layer_size) == 0:
                raise ValueError("hidden_layer_size_{} = {} "
                                 "is an empty list, "
                                 "while num_hidden_layers_{} = {} > 0"
                                 "".format(name, layer_size,
                                           name, num_layers))

            logger.error("The length of hidden_layer_size_{} = {} "
                         "does not correspond to num_hidden_layers_{} "
                         "= {}. Set hidden_layer_size_{} to "
                         "the first element = {} for all layers"
                         "".format(name, len(layer_size),
                                   name, num_layers,
                                   name, layer_size[0]))

            layer_size = layer_size[0]

        if not isinstance(layer_size, list):
            layer_size = [layer_size for _ in range(num_layers)]

        return num_layers, layer_size

    def __init__(self, component_config=None,
                 intent_dict=None,
                 intent_token_dict=None,
                 session=None,
                 graph=None,
                 intent_placeholder=None,
                 embedding_placeholder=None,
                 similarity_op=None):
        """Declare instant variables with default values"""

        super(EmbeddingIntentClassifier, self).__init__(component_config)

        # nn architecture parameters
        self._load_nn_architecture_params()
        # embedding parameters
        self._load_embedding_params()
        # regularization
        self._load_regularization_params()
        # flag if tokenize intents
        self._load_flag_if_tokenize_intents()

        # check if hidden_layer_sizes are valid
        (self.num_hidden_layers_a,
         self.hidden_layer_size_a) = self._check_hidden_layer_sizes(
                                        self.num_hidden_layers_a,
                                        self.hidden_layer_size_a,
                                        name='a')
        (self.num_hidden_layers_b,
         self.hidden_layer_size_b) = self._check_hidden_layer_sizes(
                                        self.num_hidden_layers_b,
                                        self.hidden_layer_size_b,
                                        name='b')

        # transform intents to numbers
        # encode intents with numbers
        self.intent_dict = intent_dict
        # encode words in intents with numbers
        self.intent_token_dict = intent_token_dict

        # tf related instances
        self.session = session
        self.graph = graph if graph is not None else tf.Graph()
        self.intent_placeholder = intent_placeholder
        self.embedding_placeholder = embedding_placeholder
        self.similarity_op = similarity_op

    @classmethod
    def required_packages(cls):
        # type: () -> List[Text]
        return ["tensorflow"]

    # training data helpers:
    @staticmethod
    def _create_intent_dict(training_data):
        """Create intent dictionary"""
        intent_dict = {}
        for example in training_data.intent_examples:
            intent = example.get("intent")
            if intent not in intent_dict:
                intent_dict[intent] = len(intent_dict)
        return intent_dict

    @staticmethod
    def _create_intent_token_dict(training_data, intent_split_symbol='_'):
        """Create intent token dictionary"""
        intent_token_dict = {}
        for example in training_data.intent_examples:
            intent = example.get("intent")
            for t in intent.split(intent_split_symbol):
                if t not in intent_token_dict:
                    intent_token_dict[t] = len(intent_token_dict)
        return intent_token_dict

    # data helpers:
    def _create_Y(self, training_data, num_examples):
        """Create Y

        Array that holds bag of words for tokenized intents.
        If intent_tokenization_flag = False this is one-hot vector"""

        if self.intent_tokenization_flag and self.intent_split_symbol:
            self.intent_token_dict = self._create_intent_token_dict(
                training_data,
                self.intent_split_symbol)

            Y = np.zeros([num_examples, len(self.intent_token_dict)])
            for i, example in enumerate(training_data.intent_examples):
                for t in example.get("intent").split(self.intent_split_symbol):
                    Y[i, self.intent_token_dict[t]] = 1

        else:
            Y = np.zeros([num_examples, len(self.intent_dict)])
            for i, example in enumerate(training_data.intent_examples):
                Y[i, self.intent_dict[example.get("intent")]] = 1

        return Y

    def _create_intents_for_X(self, training_data, num_examples):
        """Create intents_for_X

        Due to tokenization of the intents, special array is created
        that stores the number of the whole intent from intent_dict"""

        intents_for_X = np.zeros(num_examples, dtype=int)
        for i, example in enumerate(training_data.intent_examples):
            intents_for_X[i] = self.intent_dict[example.get("intent")]

        return intents_for_X

    def _create_encoded_intents(self):
        """Create matrix with intents encoded in rows as bag of words,
        if intent_tokenization_flag = False this is identity matrix"""

        if self.intent_token_dict:
            encoded_all_intents = np.zeros((len(self.intent_dict),
                                            len(self.intent_token_dict)))
            for key, value in self.intent_dict.items():
                for t in key.split(self.intent_split_symbol):
                    encoded_all_intents[value, self.intent_token_dict[t]] = 1

            return encoded_all_intents
        else:
            return np.eye(len(self.intent_dict))

    def _create_all_Y(self, size):
        # the matrix that encodes intents as bag of words in rows
        # if intent_tokenization = False this is identity matrix
        encoded_all_intents = self._create_encoded_intents()

        # stack encoded_all_intents on top of each other
        # to create candidates for training examples
        # to calculate training accuracy
        all_Y = np.stack([encoded_all_intents for _ in range(size)])

        return encoded_all_intents, all_Y

    def _prepare_data_for_training(self, training_data):
        """Prepare data for training"""

        X = np.stack([example.get("text_features")
                      for example in training_data.intent_examples])

        Y = self._create_Y(training_data, X.shape[0])

        intents_for_X = self._create_intents_for_X(training_data,
                                                   X.shape[0])

        encoded_all_intents, all_Y = self._create_all_Y(X.shape[0])

        helper_data = intents_for_X, encoded_all_intents, all_Y

        return X, Y, helper_data

    # tf helpers:
    def _create_tf_embed_nn(self, x_in, is_training,
                            num_layers, layer_size, name):
        """Create embed nn for layer with name"""

        reg = tf.contrib.layers.l2_regularizer(self.C2)
        x = x_in
        for i in range(num_layers):
            x = tf.layers.dense(inputs=x,
                                units=layer_size[i],
                                activation=tf.nn.relu,
                                kernel_regularizer=reg,
                                name='hidden_layer_{}_{}'.format(name, i))
            x = tf.layers.dropout(x, rate=self.droprate, training=is_training)

        x = tf.layers.dense(inputs=x,
                            units=self.embed_dim,
                            kernel_regularizer=reg,
                            name='embed_layer_{}'.format(name))
        return x

    def _tf_sim(self, a, b):
        """Define similarity"""

        if self.similarity_type == 'cosine':
            a = tf.nn.l2_normalize(a, -1)
            b = tf.nn.l2_normalize(b, -1)

        if self.similarity_type == 'cosine' or self.similarity_type == 'inner':
            sim = tf.reduce_sum(tf.expand_dims(a, 1) * b, -1)

            # similarity between intent embeddings
            sim_emb = tf.reduce_sum(b[:, 0:1, :] * b[:, 1:, :], -1)

            return sim, sim_emb
        else:
            raise NameError("Wrong similarity type {}, "
                            "should be 'cosine' or 'inner'"
                            "".format(self.similarity_type))

    def _tf_loss(self, sim, sim_emb):
        """Define loss"""

        if self.use_max_sim_neg:
            max_sim_neg = tf.reduce_max(sim[:, 1:], -1)
            loss = tf.reduce_mean(tf.maximum(0., self.mu_pos - sim[:, 0]) +
                                  tf.maximum(0., self.mu_neg + max_sim_neg))
        else:
            # create an array for mu
            mu = self.mu_neg * np.ones(self.num_neg + 1)
            mu[0] = self.mu_pos

            factors = tf.concat([-1 * tf.ones([1, 1]),
                                 tf.ones([1, tf.shape(sim)[1] - 1])], 1)
            max_margin = tf.maximum(0., mu + factors * sim)
            loss = tf.reduce_mean(tf.reduce_sum(max_margin, -1))

        max_sim_emb = tf.maximum(0., tf.reduce_max(sim_emb, -1))

        loss = (loss +
                # penalize max similarity between intent embeddings
                tf.reduce_mean(max_sim_emb) * self.C_emb +
                # add regularization losses
                tf.losses.get_regularization_loss())
        return loss

    def _create_tf_graph(self, a_in, b_in, is_training):
        """Create tf graph for training"""

        a = self._create_tf_embed_nn(a_in, is_training,
                                     self.num_hidden_layers_a,
                                     self.hidden_layer_size_a,
                                     name='a')
        b = self._create_tf_embed_nn(b_in, is_training,
                                     self.num_hidden_layers_b,
                                     self.hidden_layer_size_b,
                                     name='b')
        sim, sim_emb = self._tf_sim(a, b)
        loss = self._tf_loss(sim, sim_emb)

        return sim, loss

    # training helpers:
    def _create_batch_b(self, batch_pos_b, intent_ids, encoded_all_intents):
        """Create batch of intents, where the first is correct intent
            and the rest are wrong intents sampled randomly"""

        batch_pos_b = batch_pos_b[:, np.newaxis, :]

        # sample negatives
        batch_neg_b = np.zeros((batch_pos_b.shape[0], self.num_neg,
                                batch_pos_b.shape[-1]))
        for b in range(batch_pos_b.shape[0]):
            # create negative indexes out of possible ones
            # except for correct index of b
            negative_indexes = [i for i in range(encoded_all_intents.shape[0])
                                if i != intent_ids[b]]
            negs = np.random.choice(negative_indexes, size=self.num_neg)

            batch_neg_b[b] = encoded_all_intents[negs]

        return np.concatenate([batch_pos_b, batch_neg_b], 1)

    def _output_training_stat(self,
                              X, intents_for_X, all_Y,
                              sess, a_in, b_in, sim, is_training,
                              ep, sess_out):
        """Output training statistics"""

        train_sim = sess.run(sim, feed_dict={a_in: X,
                                             b_in: all_Y,
                                             is_training: False})

        train_acc = np.mean(np.argmax(train_sim, -1) == intents_for_X)
        logger.debug("epoch {} / {}: loss {}, train accuracy : {:.3f}"
                     "".format((ep + 1), self.epochs,
                               sess_out.get('loss'), train_acc))

    def _train_tf(self, X, Y, helper_data,
                  sess, a_in, b_in, sim,
                  loss, is_training, train_op):
        """Train tf graph"""
        sess.run(tf.global_variables_initializer())

        intents_for_X, encoded_all_intents, all_Y = helper_data

        batches_per_epoch = (len(X) // self.batch_size +
                             int(len(X) % self.batch_size > 0))
        for ep in range(self.epochs):
            indices = np.random.permutation(len(X))
            sess_out = {}
            for i in range(batches_per_epoch):
                end_idx = (i + 1) * self.batch_size
                start_idx = i * self.batch_size
                batch_a = X[indices[start_idx:end_idx]]
                batch_pos_b = Y[indices[start_idx:end_idx]]
                intents_for_b = intents_for_X[indices[start_idx:end_idx]]
                # add negatives
                batch_b = self._create_batch_b(batch_pos_b, intents_for_b,
                                               encoded_all_intents)

                sess_out = sess.run({'loss': loss, 'train_op': train_op},
                                    feed_dict={a_in: batch_a,
                                               b_in: batch_b,
                                               is_training: True})

            if logger.isEnabledFor(logging.DEBUG) and (ep + 1) % 10 == 0:
                self._output_training_stat(X, intents_for_X, all_Y,
                                           sess, a_in, b_in,
                                           sim, is_training,
                                           ep, sess_out)

    def train(self, training_data, cfg=None, **kwargs):
        # type: (TrainingData, RasaNLUModelConfig, **Any) -> None
        """Train the embedding intent classifier on a data set."""

        self.intent_dict = self._create_intent_dict(training_data)

        if len(self.intent_dict) < 2:
            logger.error("Can not train an intent classifier. "
                         "Need at least 2 different classes. "
                         "Skipping training of intent classifier.")
            return

        # check if number of negatives is less than number of intents
        logger.debug("Check if num_neg {} is smaller than "
                     "number of intents {}, "
                     "else set num_neg to the number of intents"
                     "".format(self.num_neg, len(self.intent_dict)))
        self.num_neg = min(self.num_neg, len(self.intent_dict) - 1)

        X, Y, helper_data = self._prepare_data_for_training(training_data)

        self.graph = tf.Graph()
        with self.graph.as_default():
            a_in = tf.placeholder(tf.float32, (None, X.shape[-1]),
                                  name='a')
            b_in = tf.placeholder(tf.float32, (None, None, Y.shape[-1]),
                                  name='b')
            self.embedding_placeholder = a_in
            self.intent_placeholder = b_in

            is_training = tf.placeholder_with_default(False, shape=())

            sim, loss = self._create_tf_graph(a_in, b_in, is_training)
            self.similarity_op = sim

            train_op = tf.train.AdamOptimizer().minimize(loss)

            # train tensorflow graph
            sess = tf.Session()
            self.session = sess

            self._train_tf(X, Y, helper_data,
                           sess, a_in, b_in, sim,
                           loss, is_training, train_op)

    # process helpers
    def _calculate_message_sim(self, X, all_Y):
        """Load tf graph and calculate message similarities"""

        a_in = self.embedding_placeholder
        b_in = self.intent_placeholder

        sim = self.similarity_op
        sess = self.session

        message_sim = sess.run(sim, feed_dict={a_in: X,
                                               b_in: all_Y})
        message_sim = message_sim.flatten()  # sim is a matrix

        intent_ids = message_sim.argsort()[::-1]
        message_sim[::-1].sort()

        # transform sim to python list for JSON serializing
        message_sim = message_sim.tolist()

        return intent_ids, message_sim

    def process(self, message, **kwargs):
        # type: (Message, **Any) -> None
        """Return the most likely intent and its similarity to the input."""

        intent = {"name": None, "confidence": 0.0}
        intent_ranking = []

        if self.session is None:
            logger.error("There is no trained tf.session: "
                         "component is either not trained or "
                         "didn't receive enough training data")

        else:
            # get features (bag of words) for a message
            X = message.get("text_features").reshape(1, -1)

            _, all_Y = self._create_all_Y(X.shape[0])

            # load tf graph and session
            intent_ids, message_sim = self._calculate_message_sim(X, all_Y)

            if intent_ids.size > 0:
                inv_intent_dict = {value: key
                                   for key, value in self.intent_dict.items()}

                intent = {"name": inv_intent_dict[intent_ids[0]],
                          "confidence": message_sim[0]}

                ranking = list(zip(list(intent_ids), message_sim))
                ranking = ranking[:INTENT_RANKING_LENGTH]
                intent_ranking = [{"name": inv_intent_dict[intent_idx],
                                   "confidence": score}
                                  for intent_idx, score in ranking]

        message.set("intent", intent, add_to_output=True)
        message.set("intent_ranking", intent_ranking, add_to_output=True)

    @classmethod
    def load(cls,
             model_dir=None,  # type: Text
             model_metadata=None,  # type: Metadata
             cached_component=None,  # type: Optional[Component]
             **kwargs  # type: **Any
             ):
        # type: (...) -> EmbeddingIntentClassifier

        meta = model_metadata.for_component(cls.name)

        if model_dir and meta.get("classifier_file"):
            file_name = meta.get("classifier_file")
            checkpoint = os.path.join(model_dir, file_name)
            graph = tf.Graph()
            with graph.as_default():
                sess = tf.Session()
                saver = tf.train.import_meta_graph(checkpoint + '.meta')

                saver.restore(sess, checkpoint)

                embedding_placeholder = tf.get_collection(
                    'embedding_placeholder')[0]
                intent_placeholder = tf.get_collection(
                    'intent_placeholder')[0]
                similarity_op = tf.get_collection(
                    'similarity_op')[0]

            intent_token_dict = pickle.load(open(os.path.join(
                model_dir, cls.name + "_intent_token_dict.pkl"), 'rb'))
            intent_dict = pickle.load(open(os.path.join(
                model_dir, cls.name + "_intent_dict.pkl"), 'rb'))

            return EmbeddingIntentClassifier(
                        intent_dict=intent_dict,
                        intent_token_dict=intent_token_dict,
                        session=sess,
                        graph=graph,
                        intent_placeholder=intent_placeholder,
                        embedding_placeholder=embedding_placeholder,
                        similarity_op=similarity_op)

        else:
            logger.warning("Failed to load nlu model. Maybe path {} "
                           "doesn't exist"
                           "".format(os.path.abspath(model_dir)))
            return EmbeddingIntentClassifier(meta)

    def persist(self, model_dir):
        # type: (Text) -> Dict[Text, Any]
        """Persist this model into the passed directory.
        Return the metadata necessary to load the model again."""
        if self.session is None:
            return {"classifier_file": None}

        checkpoint = os.path.join(model_dir, self.name + ".ckpt")

        try:
            os.makedirs(os.path.dirname(checkpoint))
        except OSError as e:
            # be happy if someone already created the path
            import errno
            if e.errno != errno.EEXIST:
                raise
        with self.graph.as_default():
            self.graph.clear_collection('embedding_placeholder')
            self.graph.add_to_collection('embedding_placeholder',
                                         self.embedding_placeholder)

            self.graph.clear_collection('intent_placeholder')
            self.graph.add_to_collection('intent_placeholder',
                                         self.intent_placeholder)

            self.graph.clear_collection('similarity_op')
            self.graph.add_to_collection('similarity_op',
                                         self.similarity_op)

            saver = tf.train.Saver()
            saver.save(self.session, checkpoint)

            pickle.dump(self.intent_token_dict, open(os.path.join(
                model_dir, self.name + "_intent_token_dict.pkl"), 'wb'))
            pickle.dump(self.intent_dict, open(os.path.join(
                model_dir, self.name + "_intent_dict.pkl"), 'wb'))

        return {"classifier_file": self.name + ".ckpt"}
