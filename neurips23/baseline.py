import torchvision
from rblur.models import NormalizationLayer
from rblur.models import (
    CommonModelParams, GeneralClassifier, SequentialLayers, XResNet34, XResNet18, XResNet50, WideResnet,
    ActivationLayer, BatchNorm2DLayer, FovTexVGG)
from rblur.retina_preproc import (
    RetinaBlurFilter, RetinaNonUniformPatchEmbedding, RetinaWarp, GaussianBlurLayer, GreyscaleLayer)
from rblur.supconloss import \
    TwoCropTransform
from rblur.trainers import MixedPrecisionAdversarialTrainer, LightningAdversarialTrainer
from mllib.optimizers.configs import (AdamOptimizerConfig,
                                      CosineAnnealingWarmRestartsConfig,
                                      CyclicLRConfig, LinearLRConfig,
                                      ReduceLROnPlateauConfig,
                                      SequentialLRConfig, OneCycleLRConfig, SGDOptimizerConfig)
from mllib.runners.configs import BaseExperimentConfig, TrainingParams
from mllib.tasks.base_tasks import AbstractTask
from torch import nn
from rblur.task_utils import *
from rblur.imagenet_mlp_mixer_tasks_commons import get_basic_mlp_mixer_params
from rblur.vit_models import ViTClassifier
class Cifar10CyclicLRAutoAugmentWideResNet4x22(AbstractTask):
    input_size = [3, 32, 32]
    widen_factor = 4
    depth = 22
    def get_dataset_params(self):
        p = get_cifar10_params(num_train=50_000, num_test=10_000)
        p.custom_transforms = (
            torchvision.transforms.Compose([
                torchvision.transforms.RandomCrop(32, padding=4, padding_mode='reflect'),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.AutoAugment(torchvision.transforms.AutoAugmentPolicy.CIFAR10),
                torchvision.transforms.ToTensor()
            ]),
            torchvision.transforms.ToTensor()
        )
        return p

    def get_model_params(self):
        resnet_p = WideResnet.ModelParams(WideResnet, CommonModelParams(self.input_size, 10), num_classes=10, 
                                            normalization_layer_params=NormalizationLayer.get_params(), depth=self.depth, 
                                            widen_factor=self.widen_factor)
        return resnet_p

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 60
        return BaseExperimentConfig(
            MixedPrecisionAdversarialTrainer.TrainerParams(MixedPrecisionAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-5, momentum=0.9, nesterov=True),
            OneCycleLRConfig(max_lr=0.4, epochs=nepochs, steps_per_epoch=352, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128
        )

class Cifar10CyclicLRAutoAugmentFovTexVGG(AbstractTask):
    img_size = 256
    input_size = [3, img_size, img_size]
    widen_factor = 4
    depth = 22
    def get_dataset_params(self):
        p = get_cifar10_params(num_train=50_000, num_test=10_000)
        p.custom_transforms = (
            torchvision.transforms.Compose([
                torchvision.transforms.RandomCrop(32, padding=4, padding_mode='reflect'),
                torchvision.transforms.Resize(self.img_size),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.AutoAugment(torchvision.transforms.AutoAugmentPolicy.CIFAR10),
                torchvision.transforms.ToTensor()
            ]),
            torchvision.transforms.ToTensor()
        )
        return p

    def get_model_params(self):
        p = FovTexVGG.ModelParams(FovTexVGG, CommonModelParams(self.input_size, 10), num_classes=10, permutation=None)
        return p

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 60
        return BaseExperimentConfig(
            AdversarialTrainer.TrainerParams(AdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-5, momentum=0.9, nesterov=True),
            OneCycleLRConfig(max_lr=1e-3, epochs=nepochs, steps_per_epoch=352, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128
        )

class Ecoset10CyclicLRRandAugmentXResNet2x18(AbstractTask):
    imgs_size = 224
    input_size = [3, imgs_size, imgs_size]
    widen_factor = 2
    def get_dataset_params(self) :
        p = get_ecoset10folder_params(train_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.RandomCrop(self.imgs_size),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15)
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.CenterCrop(self.imgs_size),
            ])
        return p

    def get_model_params(self):
        return XResNet18.ModelParams(XResNet18, CommonModelParams(self.input_size, 10), num_classes=10,
                                            normalization_layer_params=NormalizationLayer.get_params(),
                                            widen_factor=self.widen_factor)

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 60
        return BaseExperimentConfig(
            MixedPrecisionAdversarialTrainer.TrainerParams(MixedPrecisionAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-4, momentum=0.9, nesterov=True),
            OneCycleLRConfig(max_lr=0.4, epochs=nepochs, steps_per_epoch=376, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128
        )

class Ecoset10GreyscaleCyclicLRRandAugmentXResNet2x18(AbstractTask):
    imgs_size = 224
    input_size = [3, imgs_size, imgs_size]
    widen_factor = 2
    def get_dataset_params(self) :
        p = get_ecoset10folder_params(train_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.RandomCrop(self.imgs_size),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15),
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.CenterCrop(self.imgs_size),
            ])
        return p

    def get_model_params(self):
        gs_p = GreyscaleLayer.ModelParams(GreyscaleLayer, ndim=3)
        resnet_p = XResNet18.ModelParams(XResNet18, CommonModelParams(self.input_size, 10), num_classes=10,
                                            normalization_layer_params=NormalizationLayer.get_params(),
                                            widen_factor=self.widen_factor)
        p = GeneralClassifier.ModelParams(GeneralClassifier, self.input_size, gs_p, resnet_p)
        return p

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 60
        return BaseExperimentConfig(
            LightningAdversarialTrainer.TrainerParams(LightningAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-4, momentum=0.9, nesterov=True),
            OneCycleLRConfig(max_lr=0.4, epochs=nepochs, steps_per_epoch=376, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128
        )

class Ecoset10CyclicLRRandAugmentFovTexVGG(AbstractTask):
    imgs_size = 256
    input_size = [3, imgs_size, imgs_size]
    widen_factor = 2
    def get_dataset_params(self) :
        p = get_ecoset10folder_params(train_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.RandomCrop(self.imgs_size),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15)
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.CenterCrop(self.imgs_size),
            ])
        return p

    def get_model_params(self):
        p = FovTexVGG.ModelParams(FovTexVGG, CommonModelParams(self.input_size, 10), num_classes=10, permutation=None)
        return p

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 60
        return BaseExperimentConfig(
            MixedPrecisionAdversarialTrainer.TrainerParams(MixedPrecisionAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-4, momentum=0.9, nesterov=True),
            OneCycleLRConfig(max_lr=0.4, epochs=nepochs, steps_per_epoch=376, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128
        )

class Ecoset10RandAugmentMLPMixerS16(AbstractTask):
    input_width = 224
    def get_dataset_params(self) :
        p = get_ecoset10folder_params(train_transforms=[
                torchvision.transforms.Resize(self.input_width),
                torchvision.transforms.RandomCrop(self.input_width),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15)
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.input_width),
                torchvision.transforms.CenterCrop(self.input_width),
            ])
        return p
    
    def get_model_params(self):
        return get_basic_mlp_mixer_params([3,self.input_width,self.input_width], 10, 16, 512, 2048, 256, nn.GELU, 0., 8)
    
    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 60
        return BaseExperimentConfig(
            MixedPrecisionAdversarialTrainer.TrainerParams(MixedPrecisionAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            AdamOptimizerConfig(weight_decay=5e-5),
            OneCycleLRConfig(max_lr=0.001, epochs=nepochs, steps_per_epoch=375, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128)

class Ecoset10RandAugmentViTB16(AbstractTask):
    input_width = 224
    def get_dataset_params(self) :
        p = get_ecoset10folder_params(train_transforms=[
                torchvision.transforms.Resize(self.input_width),
                torchvision.transforms.RandomCrop(self.input_width),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15)
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.input_width),
                torchvision.transforms.CenterCrop(self.input_width),
            ])
        return p
    
    def get_model_params(self):
        return ViTClassifier.ModelParams(ViTClassifier, num_labels=10)
    
    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 60
        return BaseExperimentConfig(
            MixedPrecisionAdversarialTrainer.TrainerParams(MixedPrecisionAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            AdamOptimizerConfig(weight_decay=5e-5),
            OneCycleLRConfig(max_lr=0.001, epochs=nepochs, steps_per_epoch=375, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128)

class Ecoset10RandAugmentViTCustomSmall(Ecoset10RandAugmentViTB16):
    def get_model_params(self):
        return ViTClassifier.ModelParams(ViTClassifier, num_labels=10, hidden_size=512, num_hidden_layers=8, intermediate_size=2048, num_attention_heads=8)

class Ecoset100CyclicLRRandAugmentXResNet2x18(AbstractTask):
    imgs_size = 224
    input_size = [3, imgs_size, imgs_size]
    widen_factor = 2
    def get_dataset_params(self) :
        p = get_ecoset100folder_params(num_train=500_000, train_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.RandomCrop(self.imgs_size),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15)
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.CenterCrop(self.imgs_size),
            ])
        return p

    def get_model_params(self):
        return XResNet18.ModelParams(XResNet18, CommonModelParams(self.input_size, 100), num_classes=100,
                                            normalization_layer_params=NormalizationLayer.get_params(),
                                            widen_factor=self.widen_factor)

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 40
        return BaseExperimentConfig(
            MixedPrecisionAdversarialTrainer.TrainerParams(MixedPrecisionAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-4, momentum=0.9, nesterov=True),
            OneCycleLRConfig(max_lr=0.2, epochs=nepochs, steps_per_epoch=1839, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=256
        )

class EcosetCyclicLRRandAugmentXResNet2x18(AbstractTask):
    imgs_size = 224
    input_size = [3, imgs_size, imgs_size]
    widen_factor = 2
    def get_dataset_params(self) :
        p = get_ecoset_params(train_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.RandomCrop(self.imgs_size),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15)
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.CenterCrop(self.imgs_size),
            ])
        # Pointing to a folder with only the test set, and some dummy train and val data. 
        # Use this on workhorse to avoid delay due to slow NFS.
        # p = get_ecoset_folder_params(test_transforms=[
        #         torchvision.transforms.Resize(self.imgs_size),
        #         torchvision.transforms.CenterCrop(self.imgs_size),
        #     ])
        # p.datafolder = f'{logdir_root}/ecoset/eval_dataset_dir'
        return p

    def get_model_params(self):
        return XResNet18.ModelParams(XResNet18, CommonModelParams(self.input_size, 565), num_classes=565,
                                            normalization_layer_params=NormalizationLayer.get_params(),
                                            widen_factor=self.widen_factor)

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 25
        return BaseExperimentConfig(
            LightningAdversarialTrainer.TrainerParams(LightningAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-4, momentum=0.9, nesterov=True),
            # OneCycleLRConfig(max_lr=0.1, epochs=nepochs, steps_per_epoch=1839, pct_start=0.05, anneal_strategy='linear'),
            # OneCycleLRConfig(max_lr=0.1, epochs=nepochs, steps_per_epoch=5632, pct_start=0.1, anneal_strategy='linear'),
            OneCycleLRConfig(max_lr=0.2, epochs=nepochs, steps_per_epoch=5632, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=128
        )

class ImagenetCyclicLRRandAugmentXResNet2x18(AbstractTask):
    imgs_size = 224
    input_size = [3, imgs_size, imgs_size]
    widen_factor = 2
    def get_dataset_params(self) :
        p = get_imagenet_folder_params(num_train=1_275_000, train_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.RandomCrop(self.imgs_size),
                torchvision.transforms.RandomHorizontalFlip(),
                torchvision.transforms.RandAugment(magnitude=15)
            ],
            test_transforms=[
                torchvision.transforms.Resize(self.imgs_size),
                torchvision.transforms.CenterCrop(self.imgs_size),
            ])
        # Pointing to a folder with only the test set, and some dummy train and val data. 
        # Use this on workhorse to avoid delay due to slow NFS.
        # p.datafolder = f'{logdir_root}/imagenet/eval_dataset_dir'
        return p

    def get_model_params(self):
        return XResNet18.ModelParams(XResNet18, CommonModelParams(self.input_size, 1000), num_classes=1000,
                                            normalization_layer_params=NormalizationLayer.get_params(),
                                            widen_factor=self.widen_factor)

    def get_experiment_params(self) -> BaseExperimentConfig:
        nepochs = 25
        return BaseExperimentConfig(
            LightningAdversarialTrainer.TrainerParams(LightningAdversarialTrainer,
                TrainingParams(logdir=LOGDIR, nepochs=nepochs, early_stop_patience=50, tracked_metric='val_accuracy',
                    tracking_mode='max', scheduler_step_after_epoch=False
                )
            ),
            SGDOptimizerConfig(lr=0.2, weight_decay=5e-4, momentum=0.9, nesterov=True),
            # OneCycleLRConfig(max_lr=0.1, epochs=nepochs, steps_per_epoch=1839, pct_start=0.05, anneal_strategy='linear'),
            # OneCycleLRConfig(max_lr=0.1, epochs=nepochs, steps_per_epoch=5632, pct_start=0.1, anneal_strategy='linear'),
            OneCycleLRConfig(max_lr=0.2, epochs=nepochs, steps_per_epoch=4916, pct_start=0.2, anneal_strategy='linear'),
            logdir=LOGDIR, batch_size=64 # 4GPUS 256 batch
        )