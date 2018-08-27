from gym import spaces
import tensorflow as tf
import numpy as np
from baselines.common.input import observation_placeholder
from baselines.common.mpi_running_mean_std import RunningMeanStd
from baselines.a2c.utils import fc

def logsigmoid(a):
    return - tf.nn.softplus(-a)  # Equivalent to tf.log(tf.sigmoid(a))

def logit_bernoulli_entropy(logits):
    ent = (1. - tf.nn.sigmoid(logits)) * logits - logsigmoid(logits)
    return ent

class TransitionClassifier(object):
    def __init__(self, env, classifier_network, num_particles, classifier_entropy, normalize_observations=True):
        self.env = env
        self.ob_space, self.ac_space = env.observation_space, env.action_space
        self.num_particles = num_particles
        self.classifier_entropy = classifier_entropy
        self.Xs, self.As, self.Ls, inputs = self.make_placeholders_and_inputs()
        if normalize_observations:
            inputs, self.rms = self.normalize_inputs(inputs)
        self.grads_list, self.vars_list, self.reward_op = self.make_objectives_and_gradients(inputs, classifier_network)

    def make_placeholder(self):
        Xs, As, Ls, inputs = {}, {}, {}, {}
        for c in ['a', 'e']:
            Xs[c] = observation_placeholder(self.ob_space, name='Ob_'+c)
            As[c] = observation_placeholder(self.ac_space, name='Ac_'+c)
            Ls[c] = tf.placeholder(tf.int32, [None], name='Ls_'+c)
            if isinstance(self.ac_space, spaces.Box):
                inputs[c] = tf.concat(Xs[c], As[c], axis=1)
            elif isinstance(self.ac_space, spaces.Discrete):
                inputs[c] = Xs[c]
            else:
                raise NotImplementedError

        return Xs, As, Ls, inputs

    def normalize_inputs(self, inputs, clip_range=[-5.0, 5.0]):
        normalized_inputs = {}
        if isinstance(self.ac_space, spaces.Box):
            shape = np.array(self.ob_space.shape) + np.array(self.ac_space.shape)
        elif isinstance(self.ac_space, spaces.Discrete):
            shape = np.array(self.ob_space.shape)
        else:
            raise NotImplementedError
        rms = RunningMeanStd(shape=shape)
        for c in ['a', 'e']:
            normalized_inputs[c] = tf.clip_by_value((inputs[c] - rms.mean) / rms.std, min(clip_range), max(clip_range))

        return normalized_inputs, rms

    def make_objectives_and_gradients(self, inputs, classifier_network):
        if isinstance(self.ac_space, spaces.Box):
            num_output_units = 1
        elif isinstance(self.ac_space, spaces.Discrete):
            num_output_units = self.ac_space.n
        else:
            raise NotImplementedError

        def _make_logits(inputs, action_placeholder):
            classifier_latent, recurrent_tensors = classifier_network(inputs)
            if recurrent_tensors is not None:
                raise NotImplementedError

            logits = fc(classifier_latent, 'out', nh=num_output_units, init_scale=np.sqrt(2))
            if isinstance(self.ac_space, spaces.Discrete):
                column0 = tf.reshape(tf.range(tf.shape(action_placeholder)[0]), [-1, 1])
                column1 = tf.to_int32(action_placeholder)
                indices = tf.concat([column0, column1], axis=1)
                logits = tf.gather_nd(logits, indices)

            return logits

        gradients_list, variables_list, neg_cross_ents_list = [], [], []

        reward_op = tf.constant(0., dtype=tf.float32)

        for i in range(self.num_particles):
            for c in ['a', 'e']:
                logits, objectives = {}, {}
                with tf.variable_scope('classifier{}'.format(i), reuse=tf.AUTO_REUSE):
                    logits[c] = _make_logits(inputs, self.As[c])
                    labels = tf.zeros_like(logits[c]) if c is 'a' else tf.ones_like(logits[c])
                    neg_cross_ents = - tf.nn.sigmoid_cross_entropy_with_logits(logits=logits[c], labels=labels)

                    input_ta = tf.TensorArray(tf.float32, 0, True).split(neg_cross_ents, self.Ls[c])
                    output_ta = tf.TensorArray(tf.float32, 0, True)
                    time = tf.constant(0, dtype=tf.int32)

                    def cond(time, output_ta): return tf.less(time, tf.shape(self.Ls[c])[0])

                    def body(time, output_ta):
                        output_ta = output_ta.write(time, tf.reduce_sum(input_ta.read(time)))
                        time = time + 1
                        return time, output_ta

                    _, output_ta = tf.while_loop(cond=cond, body=body, loop_vars=[time, output_ta], parallel_iterations=100)

                    objectives[c] = tf.reduce_logsumexp(output_ta.stack())

            classifier_entropy = tf.reduce_mean(logit_bernoulli_entropy(tf.concat([logits['a'], logits['c']], axis=0)))
            sum_objective = objectives['a'] + objectives['c'] + self.classifier_entcoeff * classifier_entropy
            variables = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='classifier{}'.format(i))
            variables_list.append(variables)
            gradients = tf.gradient(sum_objective, variables)
            gradients_list.append(gradients)

            if 'MountainCar' in str(self.env):
                reward_op += tf.log(tf.nn.sigmoid(logits['a']) + 1e-8)
            else:
                reward_op += - tf.log(1. - tf.nn.sigmoid(logits['a']) + 1e-8)

        return gradients_list, variables_list, reward_op / self.num_particles

    def get_grads_and_vars(self):
        return self.grads_list, self.vars_list

    def get_reward(self, observations, actions):
        sess = tf.get_default_session()
        if len(observations.shape) == 1:
            observations = np.expand_dims(observations, 0).reshape(-1, 1)
        if len(actions.shape) == 1:
            actions = np.expand_dims(actions, 0).reshape(-1, 1)
        feed_dict = {self.Xs['a']: observations, self.As['a']: actions}

        return sess.run(self.reward_op, feed_dict)

def build_classifier(env, classifier_network, num_particles, normalize_observations=True):
    if isinstance(classifier_network, str):
        raise NotImplementedError

    def classifier_fn():
        ob_space, ac_space = env.observation_space, env.action_space

        if not isinstance(ob_space, spaces.Box):
            raise NotImplementedError

        if isinstance(ac_space, spaces.Box):
            classifier = TransitionClassifier(
                env=env,
                classifier_network=classifier_network,
                num_particles=num_particles,
                normalize_observations=normalize_observations
                )
        elif isinstance(ac_space, spaces.Discrete):
            raise NotImplementedError
        else:
            raise NotImplementedError

        return classifier

    return classifier_fn()
