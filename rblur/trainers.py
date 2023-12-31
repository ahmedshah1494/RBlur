from copy import deepcopy
from enum import Enum, auto
from hashlib import sha224
import os
import shutil
from time import time
from typing import List, Type, Union, Tuple
try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal
from attrs import define, field
from mllib.trainers.base_trainers import Trainer as _Trainer
from mllib.trainers.base_trainers import PytorchLightningTrainer
from mllib.trainers.base_trainers import MixedPrecisionTrainerMixin
# from mllib.trainers.pl_trainer import PytorchLightningLiteTrainerMixin, LightningLiteParams
from mllib.runners.configs import TrainingParams
from mllib.utils.metric_utils import compute_accuracy, get_preds_from_logits
from mllib.param import BaseParameters
from numpy import iterable
import torch
import numpy as np
import torchattacks
from einops import rearrange

import pytorch_lightning as pl

from mllib.adversarial.attacks import AbstractAttackConfig, FoolboxAttackWrapper, FoolboxCWL2AttackWrapper, AutoAttackkWrapper
from mllib.adversarial.randomized_smoothing.core import Smooth
from rblur.pruning import PruningMixin
from rblur.utils import aggregate_dicts, merge_iterables_in_dict, write_json, write_pickle, load_json, recursive_dict_update, load_pickle

import torchmetrics
from tqdm import tqdm

from itertools import product


def get_hash(x: Union[List, np.ndarray, torch.Tensor]):
    if isinstance(x, torch.Tensor):
        x = x.cpu().detach().numpy()
    elif isinstance(x, list):
        x = np.array(x)
    elif not isinstance(x, np.ndarray):
        raise ValueError(f'Type {type(x)} is unsupported for input x')
    return sha224(x.tobytes()).hexdigest()

@define(slots=False)
class AdversarialParams:
    training_attack_params: AbstractAttackConfig = None
    testing_attack_params: List[AbstractAttackConfig] = [None]

class AdversarialTrainer(_Trainer, PruningMixin):    
    @define(slots=False)    
    class TrainerParams(BaseParameters):
        training_params: Type[TrainingParams] = field(factory=TrainingParams)
        adversarial_params: Type[AdversarialParams] = field(factory=AdversarialParams)

    @classmethod
    def get_params(cls):
        return cls.TrainerParams(cls)

    def __init__(self, params: TrainerParams, *args, **kwargs):
        super().__init__(params, *args, **kwargs)
        print(self.model)
        self.params = params
        self.training_adv_attack = self._maybe_get_attacks(params.adversarial_params.training_attack_params)
        if isinstance(self.training_adv_attack, tuple):
            self.training_adv_attack = self.training_adv_attack[1]
        self.testing_adv_attacks = self._maybe_get_attacks(params.adversarial_params.testing_attack_params)
        self.data_and_pred_filename = 'data_and_preds.pkl'
        self.metrics_filename = 'metrics.json'

    def _get_attack_from_params(self, p: Union[AbstractAttackConfig, Tuple[str, AbstractAttackConfig]]):
        if isinstance(p, tuple):
            name, p = p
        else:
            name = None
        if p is not None:
            if p.model is None:
                p.model = self.model.eval()
            return name, p._cls(p.model, **(p.asdict()))
        else:
            return name, None

    def _maybe_get_attacks(self, attack_params: Union[AbstractAttackConfig, List[AbstractAttackConfig]]):
        if attack_params is None:
            attack = ('',None)
        else:
            if iterable(attack_params):
                attack = [self._get_attack_from_params(p) for p in attack_params]
            else:
                attack = self._get_attack_from_params(attack_params)
        return attack
    
    def _maybe_attack_batch(self, batch, adv_attack):
        x,y = batch
        if adv_attack is not None:
            if x.dim() == 5:
                y_ = torch.repeat_interleave(y, x.shape[1])
                x = rearrange(x, 'b n c h w -> (b n) c h w')
                x = adv_attack(x, y_)
                x = rearrange(x, '(b n) c h w -> b n c h w', b = len(y))
            else:
                x = adv_attack(x, y)
        return x,y

    def train_step(self, batch, batch_idx):
        batch = self._maybe_attack_batch(batch, self.training_adv_attack)
        return super().train_step(batch, batch_idx)

    def test_step(self, batch, batch_idx):
        test_eps = [(p.eps if p is not None else 0.) for p in self.params.adversarial_params.testing_attack_params]
        test_pred = {}
        adv_x = {}
        test_loss = {}
        test_acc = {}
        test_logits = {}
        for name, atk in self.testing_adv_attacks:
            if isinstance(atk, FoolboxAttackWrapper):
                eps = atk.run_kwargs.get('epsilons', [float('inf')])[0]
            elif isinstance(atk, torchattacks.attack.Attack):
                eps = atk.eps
            elif atk is None:
                eps = 0.
            else:
                raise NotImplementedError(f'{type(atk)} is not supported')
            x,y = self._maybe_attack_batch(batch, atk)

            logits, loss = self._get_outputs_and_loss((x, y))
            logits = logits.detach().cpu()
            
            y = y.detach().cpu()
            acc, _ = compute_accuracy(logits, y)

            preds = get_preds_from_logits(logits)
            loss = loss.mean().detach().cpu()

            test_pred[eps] = preds.numpy().tolist()
            adv_x[eps] = x.detach().cpu().numpy()
            test_loss[eps] = loss
            test_acc[eps] = acc
            test_logits[eps] = logits.numpy()
        metrics = {f'test_acc_{k}':v for k,v in test_acc.items()}
        return {'preds':test_pred, 'labels':y.numpy().tolist(), 'inputs': adv_x, 'logits':test_logits}, metrics
    
    def test_epoch_end(self, outputs, metrics):
        outputs = self._maybe_gather_all(outputs)
        outputs = [{k: v.cpu().detach().numpy() if isinstance(v, torch.Tensor) else v for k,v in o.items()} for o in outputs]
        metrics = self._maybe_gather_all(metrics)
        metrics = {k: v.cpu().detach().numpy().tolist() if isinstance(v, torch.Tensor) else v for k,v in metrics.items()}
        outputs = aggregate_dicts(outputs)
        test_eps = [(p.eps if p is not None else 0.) for p in self.params.adversarial_params.testing_attack_params]
        new_outputs = aggregate_dicts(outputs)
        new_outputs = merge_iterables_in_dict(new_outputs)
        
        test_acc = {}
        for i,eps in enumerate(test_eps):
            acc = (np.array(new_outputs['preds'][eps]) == np.array(new_outputs['labels'])).astype(float).mean()
            test_acc[eps] = acc
        new_outputs['test_acc'] = test_acc

        print('test metrics:')
        print(metrics)
        _, train_metrics = self._batch_loop(self.val_step, self.train_loader, 0, logging=False)
        train_metrics = self._maybe_gather_all(train_metrics)
        train_metrics = {k: v.cpu().detach().numpy().tolist() if isinstance(v, torch.Tensor) else v for k,v in train_metrics.items()}
        print(train_metrics)
        for k in train_metrics:
            train_metrics[k.replace('val', 'train')] = train_metrics.pop(k)
        if self.is_rank_zero:
            train_metrics = aggregate_dicts(train_metrics)
            self.save_logs_after_test(train_metrics, outputs)
        return new_outputs, metrics
    
    def train(self):
        metrics = super().train()
        val_acc = metrics['val_accuracy']
        # if val_acc > 0.12:
        #     self.prune()

    def prune(self):
        self.iterative_pruning_wrapper(0, self.l1_unstructured_pruning_with_retraining, 0.1)
    
    def save_training_logs(self, train_acc, test_accs):
        metrics = {
            'train_acc':train_acc,
            'test_accs':test_accs,
        }
        write_json(metrics, os.path.join(self.logdir, self.metrics_filename))

    def save_data_and_preds(self, preds, labels, inputs, logits):
        d = {}
        for k in preds.keys():
            d[k] = {
                'X': inputs[k],
                'Y': labels,
                'Y_pred': preds[k],
                'logits': logits[k]
            }
        write_pickle(d, os.path.join(self.logdir, self.data_and_pred_filename))
    
    def save_source_dir(self):
        if not os.path.exists(os.path.join(self.logdir, 'source')):
            shutil.copytree(os.path.dirname(__file__), os.path.join(self.logdir, 'source'))
    
    def save_logs_after_test(self, train_metrics, test_outputs):
        self.save_training_logs(train_metrics['train_accuracy'], test_outputs['test_acc'])
        self.save_data_and_preds(test_outputs['preds'], test_outputs['labels'], test_outputs['inputs'], test_outputs['logits'])
        self.save_source_dir()

    def test(self):
        self.testing_adv_attacks = self._maybe_get_attacks(self.params.adversarial_params.testing_attack_params)
        test_outputs, test_metrics = self.test_loop(post_loop_fn=self.test_epoch_end)
        
    def _log(self, logs, step):
        if self.is_rank_zero:
            for k,v in logs.items():
                if isinstance(v, dict):
                    self.logger.add_scalars(k, v, global_step=step)
                elif not iterable(v):
                    self.logger.add_scalar(k, v, global_step=step)

def compute_adversarial_success_rate(clean_preds, preds, labels, target_labels):
    if (labels == target_labels).all():
        adv_succ = ((clean_preds == labels) & (preds != labels)).astype(float).mean()
    else:
        adv_succ = (preds == target_labels).astype(float).mean()
    return adv_succ

def update_and_save_logs(logdir, outfilename, load_fn, write_fn, save_fn, *save_fn_args, **save_fn_kwargs):
    outfile = os.path.join(logdir, outfilename)
    if os.path.exists(outfile):
        old_metrics = load_fn(outfile)
        tmpoutfile = os.path.join(logdir, '.'+outfilename)
        shutil.copy(outfile, tmpoutfile)
        save_fn(*save_fn_args, **save_fn_kwargs)
        new_metrics = load_fn(outfile)
        recursive_dict_update(new_metrics, old_metrics)
        write_fn(old_metrics, outfile)
    else:
        save_fn(*save_fn_args, **save_fn_kwargs)

def save_pred_and_label_csv(logdir, outfile, preds, labels, logits, atk_norms):
    for atkname in preds.keys():
        sorted_logits = np.argsort(logits[atkname], 1)
        label_ranks = []
        for sl, l in zip(sorted_logits, labels):
            r = sorted_logits.shape[1] - sl.tolist().index(l) - 1
            label_ranks.append(r)
        with open(os.path.join(logdir, f'{atkname}_{outfile}'), 'w') as f:
            f.write('L,P1,P2,P3,P4,P5,R,norm\n')
            for p,l,r,sl,nrm in zip(preds[atkname], labels, label_ranks, sorted_logits, atk_norms[atkname]):
                f.write(f'{l},{p},{sl[-2]},{sl[-3]},{sl[-4]},{sl[-5]},{r},{nrm}\n')

def save_pred_and_label_csv_2(logdir, outfile, preds, labels, batch_idx):
    mode = 'a' if batch_idx > 0 else 'w'
    for atkname in preds.keys():
        with open(os.path.join(logdir, f'{atkname}_{outfile}'), mode) as f:
            for p,l in zip(preds[atkname], labels):
                f.write(f'{l},{p}\n')

def save_logits(logdir, outfile, labels, logits):
    for atkname, atklogits in logits.items():
        atklogits = atklogits.astype(np.float16)
        np.savez_compressed(os.path.join(logdir, f'{atkname}_{outfile}'), labels=labels, logits=atklogits)

class MixedPrecisionAdversarialTrainer(MixedPrecisionTrainerMixin, AdversarialTrainer):
    pass
        
class MultiAttackEvaluationTrainer(AdversarialTrainer):
    def __init__(self, params, *args, **kwargs):
        super().__init__(params, *args, **kwargs)
        self.metrics_filename = 'adv_metrics.json'
        self.data_and_pred_filename = 'adv_data_and_preds.pkl'
        self.per_sample_logdir = os.path.join(self.logdir, 'per_sample_adv_attack_results')
        self.per_attack_logdir = os.path.join(self.logdir, 'per_attack_results')
        if not os.path.exists(self.per_sample_logdir):
            os.makedirs(self.per_sample_logdir)
        if not os.path.exists(self.per_attack_logdir):
            os.makedirs(self.per_attack_logdir)

    def _maybe_initialize_logger(self):
        self.global_step = 0
        return
    
    def _log(self, logs, step):
        return

    def save_logs_after_test(self, train_metrics, test_outputs):
        update_and_save_logs(self.logdir, self.metrics_filename, load_json, write_json, self.save_training_logs, 
                                train_metrics['train_accuracy'], test_outputs['test_acc'])
        save_pred_and_label_csv(self.per_attack_logdir, 'label_and_preds.csv', test_outputs['preds'], test_outputs['labels'], test_outputs['logits'], test_outputs['atk_norms'])
        # save_logits(self.per_attack_logdir, 'logits.npz', test_outputs['labels'], test_outputs['logits'])
        # update_and_save_logs(self.logdir, self.data_and_pred_filename, load_pickle, write_pickle, self.save_data_and_preds,
        #                         test_outputs['preds'], test_outputs['labels'], test_outputs['inputs'], test_outputs['logits'])
        # self.save_training_logs(train_metrics['train_accuracy'], test_outputs['test_acc'])
        # self.save_data_and_preds(test_outputs['preds'], test_outputs['labels'], test_outputs['inputs'], test_outputs['logits'])
        self.save_source_dir()        

    def test_epoch_end(self, outputs, metrics):
        outputs = aggregate_dicts(outputs)
        new_outputs = aggregate_dicts(outputs)
        new_outputs = merge_iterables_in_dict(new_outputs)
        labels = np.array(new_outputs['labels'])
        test_acc = {}
        adv_succ = {}
        for k in new_outputs['preds'].keys():
            target_labels = np.array(new_outputs['target_labels'][k])
            preds = np.array(new_outputs['preds'][k])
            clean_preds = np.array(new_outputs['preds'][sorted(new_outputs['preds'].keys())[0]])
            acc = (preds == labels).astype(float).mean()
            test_acc[k] = acc
            adv_succ[k] = compute_adversarial_success_rate(clean_preds, preds, labels, target_labels)
        new_outputs['test_acc'] = test_acc
        new_outputs['adv_succ'] = adv_succ
        write_json(adv_succ, os.path.join(self.logdir, 'adv_succ.json'))

        print('test metrics:')
        print(metrics)
        self.save_logs_after_test({'train_accuracy': 0.}, outputs)
        return new_outputs, metrics

    def test_step(self, batch, batch_idx):
        clean_x = batch[0].clone()

        test_pred = {}
        adv_x = {}
        test_loss = {}
        test_acc = {}
        test_logits = {}
        target_labels = {}
        test_atk_norm = {}
        for name, atk in self.testing_adv_attacks:
            if isinstance(atk, FoolboxCWL2AttackWrapper):
                eps = atk.attack.confidence
            elif isinstance(atk, FoolboxAttackWrapper):
                eps = atk.run_kwargs.get('epsilons', [float('inf')])[0]
            elif isinstance(atk, AutoAttackkWrapper):
                eps = atk.attack.epsilon
            # elif isinstance(atk, torchattacks.attack.Attack):
            elif hasattr(atk, 'eps'):
                eps = atk.eps
            else:
                raise NotImplementedError(f'{type(atk)} is not supported')
            atk_name = f"{atk.__class__.__name__ if name is None else name}-{eps}"
            # if batch_idx < 1119:
            #     logits = torch.rand(batch[0].shape[0], 10).detach().cpu()
            #     x, y = batch
            #     loss = torch.rand(batch[0].shape[0])
            # else:
            adv_batch = self._maybe_attack_batch(batch, atk if eps > 0 else None)
            x, y = adv_batch[0], adv_batch[1]
            logits, loss = self._get_outputs_and_loss(*adv_batch)
            logits = logits.detach().cpu()
            
            y = y.detach().cpu()
            atk_norm = torch.flatten(x-clean_x, 1).norm(2, 1)
            acc, _ = compute_accuracy(logits, y)
            if isinstance(atk, torchattacks.attack.Attack) and atk._targeted:
                y_tgt = atk._get_target_label(*batch)
            else:
                y_tgt = y

            preds = get_preds_from_logits(logits)
            loss = loss.mean().detach().cpu()
            test_pred[atk_name] = preds.numpy().tolist()
            adv_x[atk_name] = x.detach().cpu().numpy()
            test_loss[atk_name] = loss
            test_acc[atk_name] = acc
            test_logits[atk_name] = logits.numpy()
            target_labels[atk_name] = y_tgt.detach().cpu().numpy().tolist()
            test_atk_norm[atk_name] = atk_norm.detach().cpu().numpy().tolist()
            # self.save_per_sample_results(atk_name, clean_x.detach().cpu().numpy(), adv_x[atk_name], y.numpy().tolist(), test_pred[atk_name])
            save_pred_and_label_csv_2(self.per_attack_logdir, 'label_and_preds_2.csv', test_pred, y.numpy().tolist(), batch_idx)
        metrics = {f'test_acc_{k}':v for k,v in test_acc.items()}
        return {'preds':test_pred, 'labels':y.numpy().tolist(), 'inputs': 0., 'target_labels':target_labels, 'logits': test_logits, 'atk_norms':test_atk_norm}, metrics
    
    def save_per_sample_results(self, atk_name, X, adv_X, Y, P):
        for x, adv_x, y, p in zip(X, adv_X, Y, P):
            h = get_hash(x[0])
            np.savez(f'{self.per_sample_logdir}/adv_result_{atk_name}_{h}_input.npz', x=x, adv_x=adv_x)
            r = {'y': int(y), 'y_pred': int(p)}
            write_json(r, f'{self.per_sample_logdir}/adv_result_{atk_name}_{h}_output.json')

class AnnotatedMultiAttackEvaluationTrainer(MultiAttackEvaluationTrainer):
    def _maybe_attack_batch(self, batch, adv_attack):
        x,y,l = batch
        x,y = super()._maybe_attack_batch((x,y), adv_attack)
        return x,y,l
    
    def _get_outputs_and_loss(self, batch):
        def set_param(p:BaseParameters, param, value):
            if hasattr(p, param):
                setattr(p, param, value)
            else:
                d = p.asdict(recurse=False)
                for v in d.values():
                    if isinstance(v, BaseParameters):
                        set_param(v, param, value)
                    elif np.iterable(v):
                        for x in v:
                            if isinstance(x, BaseParameters):
                                set_param(x, param, value)
            return p

        x,y,bb = batch
        assert x.shape[0] == 1
        l = torch.stack([x.shape[2]*(bb[:,0] + bb[:,2])/2, x.shape[3]*(bb[:,1] + bb[:,3])/2], 1).int().detach().cpu().numpy()
        l = l.squeeze()

        l = 800 - l
        set_param(self.model.params, 'loc_mode', 'const')
        set_param(self.model.params, 'loc', l)
        return self.model.compute_loss(x, y)

@define(slots=False)
class RandomizedSmoothingParams:
    num_classes:int = None
    sigmas: List[float] = None
    batch: int = 1000
    N0: int = 100
    N: int =  100_000
    alpha: float = 0.001
    mode: Literal['certify', 'predict'] = 'certify'
    start_idx: int = 0
    end_idx: int = np.inf

class RandomizedSmoothingEvaluationTrainer(_Trainer):
    @define(slots=False)    
    class TrainerParams(BaseParameters):
        training_params: Type[TrainingParams] = field(factory=TrainingParams)
        randomized_smoothing_params: RandomizedSmoothingParams = field(factory=RandomizedSmoothingParams)
        exp_name: str = ''
    
    def __init__(self, params: TrainerParams, *args, **kwargs):
        super().__init__(params, *args, **kwargs)
        print(self.model)
        self.params = params
        self.smoothed_models = [Smooth(self.model, self.params.randomized_smoothing_params.num_classes, s) for s in self.params.randomized_smoothing_params.sigmas]
        self.metrics_filename = 'randomized_smoothing_metrics.json'
        self.data_and_pred_filename = 'randomized_smoothing_preds_and_radii.pkl'
        self.per_sample_logdir = os.path.join(self.logdir, 'per_sample_randomized_smoothing_results')
        if not os.path.exists(self.per_sample_logdir):
            os.makedirs(self.per_sample_logdir)
    
    def _single_sample_step(self, smoothed_model, x):
        if self.params.randomized_smoothing_params.mode == 'certify':
            return smoothed_model.certify(x, self.params.randomized_smoothing_params.N0,
                                self.params.randomized_smoothing_params.N,
                                self.params.randomized_smoothing_params.alpha,
                                self.params.randomized_smoothing_params.batch)
        elif self.params.randomized_smoothing_params.mode == 'predict':
            return (smoothed_model.predict(x,self.params.randomized_smoothing_params.N,
                                self.params.randomized_smoothing_params.alpha,
                                self.params.randomized_smoothing_params.batch), 0.)
        else:
            raise ValueError(f'RandomizedSmoothingParams.mode must be either "certify" or "predict" but got {self.params.randomized_smoothing_params.mode}')

    def test_step(self, batch, batch_idx):
        x, y = batch
        preds = {}
        radii = {}
        acc = {}
        y = y.detach().cpu().numpy()
        for smoothed_model in self.smoothed_models:
            _preds = []
            _radii = []
            num_correct = 0
            print(f'start_idx:{self.params.randomized_smoothing_params.start_idx}\t end_idx:{self.params.randomized_smoothing_params.end_idx}')
            for i,(y_,x_) in tqdm(enumerate(zip(y, x))):
                if (i < self.params.randomized_smoothing_params.start_idx) or (i > self.params.randomized_smoothing_params.end_idx):
                    print(f'skipping {i}')
                    continue
                p,r = self._single_sample_step(smoothed_model, x_)
                self.save_single_sample_results(get_hash(x_[0]), f'{self.params.exp_name}{smoothed_model.sigma}', y_, p, r)
                _preds.append(p)
                _radii.append(r)
                num_correct += int(y_ == p)
                print(i, num_correct/(i+1))
            # _preds = torch.stack(_preds)
            # _radii = torch.stack(_radii)
            preds[f'{self.params.exp_name}{smoothed_model.sigma}'] = _preds#.detach().cpu().numpy().tolist()
            radii[f'{self.params.exp_name}{smoothed_model.sigma}'] = _radii#.detach().cpu().numpy().tolist()
            acc[f'{self.params.exp_name}{smoothed_model.sigma}'] = num_correct / (self.params.randomized_smoothing_params.end_idx - self.params.randomized_smoothing_params.start_idx)
        metrics = {f'test_acc_{k}':v for k,v in acc.items()}

        return {'preds': preds, 'radii': radii, 'labels':y.tolist(), 'inputs':x.detach().cpu().numpy()}, metrics

    def save_single_sample_results(self, i, name, y, y_pred, radius):
        r = {'Y':int(y), 'Y_pred':int(y_pred), 'radius': float(radius)}
        write_json(r, f'{self.per_sample_logdir}/rs_result_{name}_{i}.json')
    
    def save_training_logs(self, train_acc, test_accs):
        metrics = {
            'train_acc':train_acc,
            'test_accs':test_accs,
        }
        write_json(metrics, os.path.join(self.logdir, self.metrics_filename))

    def save_data_and_preds(self, preds, labels, inputs, radii):
        d = {
            'X': inputs,
            'Y': labels,
            'preds_and_radii': {}
        }
        for k in preds.keys():
            d['preds_and_radii'][k] = {
                'Y_pred': preds[k],
                'radii': radii[k]
            }
        write_pickle(d, os.path.join(self.logdir, self.data_and_pred_filename))
    
    def save_logs_after_test(self, train_metrics, test_outputs):
        update_and_save_logs(self.logdir, self.metrics_filename, load_json, write_json, self.save_training_logs, 
                                train_metrics['train_accuracy'], test_outputs['test_acc'])
        update_and_save_logs(self.logdir, self.data_and_pred_filename, load_pickle, write_pickle, self.save_data_and_preds, 
                                test_outputs['preds'], test_outputs['labels'], test_outputs['inputs'], test_outputs['radii'])

    def test_epoch_end(self, outputs, metrics):
        new_outputs = aggregate_dicts(outputs)
        new_outputs = merge_iterables_in_dict(new_outputs)
        test_acc = {}
        for sigma, preds in new_outputs['preds'].items():
            acc = (np.array(preds) == np.array(new_outputs['labels'])).astype(float).mean()
            test_acc[sigma] = acc
        new_outputs['test_acc'] = test_acc
        return new_outputs, metrics
    
    def test(self):
        test_outputs, test_metrics = self.test_loop(post_loop_fn=self.test_epoch_end)
        print('test metrics:')
        print(test_metrics)
        self.save_logs_after_test({'train_accuracy': 0.}, test_outputs)


class LightningAdversarialTrainer(PytorchLightningTrainer, PruningMixin):
    @define(slots=False)    
    class TrainerParams(BaseParameters):
        training_params: Type[TrainingParams] = field(factory=TrainingParams)
        adversarial_params: Type[AdversarialParams] = field(factory=AdversarialParams)

    @classmethod
    def get_params(cls):
        return cls.TrainerParams(cls)

    def __init__(self, params: TrainerParams, *args, **kwargs):
        super().__init__(params, *args, **kwargs)
        print(self.model)
        self.params = params
        self.training_adv_attack = None
        # self.training_adv_attack = self._maybe_get_attacks(params.adversarial_params.training_attack_params)
        # if isinstance(self.training_adv_attack, tuple):
        #     self.training_adv_attack = self.training_adv_attack[1]
        self.testing_adv_attacks = self._maybe_get_attacks(params.adversarial_params.testing_attack_params)
        self.test_accuracy_trackers = torch.nn.ModuleList([torchmetrics.Accuracy() for a in self.testing_adv_attacks])
        self.data_and_pred_filename = 'data_and_preds.pkl'
        self.metrics_filename = 'metrics.json'
        
    def _get_attack_from_params(self, p: Union[AbstractAttackConfig, Tuple[str, AbstractAttackConfig]]):
        if isinstance(p, tuple):
            name, p = p
        else:
            name = None
        if p is not None:
            if p.model is None:
                p.model = self.model.eval()
            return name, p._cls(p.model, **(p.asdict()))
        else:
            return name, None

    def _maybe_get_attacks(self, attack_params: Union[AbstractAttackConfig, List[AbstractAttackConfig]]):
        if attack_params is None:
            attack = ('',None)
        else:
            if iterable(attack_params):
                attack = [self._get_attack_from_params(p) for p in attack_params]
            else:
                attack = self._get_attack_from_params(attack_params)
        return attack
    
    def _maybe_attack_batch(self, batch, adv_attack):
        x,y = batch[:2]
        if adv_attack is not None:
            if x.dim() == 5:
                y_ = torch.repeat_interleave(y, x.shape[1])
                x = rearrange(x, 'b n c h w -> (b n) c h w')
                x = adv_attack(x, y_)
                x = rearrange(x, '(b n) c h w -> b n c h w', b = len(y))
            else:
                x = adv_attack(x, y)
        batch = (x,y, *(batch[2:]))
        return batch

    def training_step(self, batch, batch_idx):
        if self.training_adv_attack is None:
            self.training_adv_attack = self._maybe_get_attacks(self.params.adversarial_params.training_attack_params)
            if isinstance(self.training_adv_attack, tuple):
                self.training_adv_attack = self.training_adv_attack[1]
        batch = self._maybe_attack_batch(batch, self.training_adv_attack)
        return super().training_step(batch, batch_idx)
    
    def validation_step(self, batch, batch_idx):
        return super().validation_step(batch, batch_idx)

    def test_step(self, batch, batch_idx):
        test_eps = [(p.eps if p is not None else 0.) for p in self.params.adversarial_params.testing_attack_params]
        test_pred = {}
        adv_x = {}
        test_loss = {}
        test_acc = {}
        test_logits = {}
        for (name, atk), A in zip(self.testing_adv_attacks, self.test_accuracy_trackers):
            if isinstance(atk, FoolboxAttackWrapper):
                eps = atk.run_kwargs.get('epsilons', [float('inf')])[0]
            elif isinstance(atk, torchattacks.attack.Attack):
                eps = atk.eps
            elif atk is None:
                eps = 0.
            else:
                raise NotImplementedError(f'{type(atk)} is not supported')
            x,y = self._maybe_attack_batch(batch, atk)

            logits, loss = self._get_outputs_and_loss(x, y)
            logits = logits.detach()            
            y = y.detach()
            preds = get_preds_from_logits(logits)
            A(preds, y)

            loss = loss.mean().detach()

            test_pred[eps] = preds
            adv_x[eps] = x.detach()
            test_loss[eps] = loss
            test_acc[eps] = A.compute()
            test_logits[eps] = logits
        metrics = {f'test_acc_{k}':v for k,v in test_acc.items()}
        return {'logs': metrics}

    def test_epoch_end(self, outputs):
        super().test_epoch_end(outputs)
        outputs = aggregate_dicts(outputs)
        outputs = merge_iterables_in_dict(outputs)
        outputs['test_acc'] = outputs.pop('logs')
        outputs['test_acc'] = {k: float(v.mean().cpu()) for k,v in outputs['test_acc'].items()}
        print('test metrics:')
        print(outputs)
        if self.global_rank == 0:
            train_metrics = {'train_accuracy': 0.}
            self.save_logs_after_test(train_metrics, outputs)
        return outputs
    
    def save_training_logs(self, train_acc, test_accs):
        metrics = {
            'train_acc':train_acc,
            'test_accs':test_accs,
        }
        write_json(metrics, os.path.join(self.logdir, self.metrics_filename))

    def save_data_and_preds(self, preds, labels, inputs, logits):
        d = {}
        for k in preds.keys():
            d[k] = {
                'X': inputs[k],
                'Y': labels,
                'Y_pred': preds[k],
                'logits': logits[k]
            }
        write_pickle(d, os.path.join(self.logdir, self.data_and_pred_filename))
    
    def save_source_dir(self):
        if not os.path.exists(os.path.join(self.logdir, 'source')):
            shutil.copytree(os.path.dirname(__file__), os.path.join(self.logdir, 'source'))
    
    def save_logs_after_test(self, train_metrics, test_outputs):
        self.save_training_logs(train_metrics['train_accuracy'], test_outputs['test_acc'])
        self.save_source_dir()
        
