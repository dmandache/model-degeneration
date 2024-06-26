import torch
from torch.utils.data import Dataset, ConcatDataset
from torchvision import datasets
from torchmetrics.image.fid import FrechetInceptionDistance
from torchmetrics.image.inception import InceptionScore
from pythae.data.datasets import DatasetOutput
from pythae.models import *
from pythae.trainers import BaseTrainerConfig, BaseTrainer
from pythae.pipelines.training import TrainingPipeline
from pythae.trainers.training_callbacks import WandbCallback
from pythae.samplers import *
from pythae.models.nn.benchmarks.mnist import *
from pythae.models.nn.default_architectures import *
from utils.models import Encoder_VAE_TinyMLP, Decoder_AE_TinyMLP
from utils.models import count_parameters
from utils.data import sample_indices
import wandb
import argparse
import random
from datetime import datetime
import pandas as pd
import os

_ = torch.manual_seed(42)

## Group runs by by experiment
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
os.environ["WANDB_RUN_GROUP"] = f"experiment_{timestamp}"
LOG_DIR = f'experiments/{timestamp}'

## Args dictionary
model_dict = {
    'vae': VAE,
    'rhvae': RHVAE
    }

architecture_dict = {
    'tiny':
        {
        'encoder': Encoder_VAE_TinyMLP,
        'decoder': Decoder_AE_TinyMLP,
        },
    'mlp':
        {
        'encoder': Encoder_VAE_MLP,
        'decoder': Decoder_AE_MLP,
        },
    'convnet':
        {
        'encoder': Encoder_Conv_VAE_MNIST,
        'decoder': Decoder_Conv_AE_MNIST,
        },
    'resnet':
        {
        'encoder': Encoder_ResNet_VAE_MNIST,
        'decoder': Decoder_ResNet_AE_MNIST,
        },
    }

    
if __name__ == '__main__':

    # Argument Parser
    parser = argparse.ArgumentParser() #description='Train a RHVAE with synthetic data generation.')
    parser.add_argument('--input_dim', type=int, default=28, help='Dimensionality of the input data')
    parser.add_argument('--latent_dim', type=int, default=2, help='Dimensionality of the latent space')
    parser.add_argument('--n_runs', type=int, default=3, help='Number of degenerating runs')
    parser.add_argument('--n_train', type=int, default=20, help='Number of training samples per class')
    parser.add_argument('--n_test', type=int, default=20, help='Number of test samples per class')
    parser.add_argument('--k', type=int, default=200, help='Number of synthetic data samples to generate at each iteration')
    parser.add_argument('--sampler', choices=['normal', 'gmm', 'rhvae'], default='rhvae', help='Sampler type for generating synthetic data')
    parser.add_argument('--architecture', choices=['convnet','resnet', 'mlp', 'tiny'], default='tiny', help='Model Architecture')
    parser.add_argument('--model', choices=['rhvae','vae'], default='rhvae', help='VAE Model')
    parser.add_argument('--loss', choices=['bce','mse'], default='bce', help='Recosntruction loss [BCE or MSE]')
    parser.add_argument('--n_epochs', type=int, default=50, help='Number of training epochs for each run')
    parser.add_argument('--lr', type=float, default=1e-3, help='Learning rate')
    parser.add_argument('--batch_size', type=int, default=1000, help='Batch size (-1 = entire dataset)')
    args = parser.parse_args()

    # Set device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # Real data loader
    mnist_trainset = datasets.MNIST(root='./data', train=True, download=True, transform=None)
    
    train_indeces = sample_indices(mnist_trainset.targets, k=args.n_train, seed=42)
    remaining_indeces = list(set(range(len(mnist_trainset.targets)))-set(train_indeces))
    eval_indeces = sample_indices(mnist_trainset.targets[remaining_indeces], k=args.n_test, seed=42)

    train_dataset = mnist_trainset.data[train_indeces].reshape(-1, 1, args.input_dim, args.input_dim,) / 255.
    eval_dataset = mnist_trainset.data[eval_indeces].reshape(-1, 1, args.input_dim, args.input_dim) / 255.
    print(train_dataset.shape, eval_dataset.shape)

    #real_data = train_dataset.clone()

    train_dataset = train_dataset.to(device)
    eval_dataset = eval_dataset.to(device)

    # Check Args
    batch_size_train = len(train_dataset) if args.batch_size == -1 else args.batch_size
    batch_size_eval = len(eval_dataset) if args.batch_size == -1 else args.batch_size

    # Model Config
    if args.model == 'rhvae':
        model_config = RHVAEConfig(
            input_dim=(1, args.input_dim, args.input_dim),
            latent_dim=args.latent_dim,
            reconstruction_loss=args.loss,
            # n_lf=3,
            # eps_lf=1e-3,
            # beta_zero=0.3,
            # temperature=0.8,
            # regularization=1e-3
        )
    elif args.model == 'vae':
        model_config = VAEConfig(
            input_dim=(1, args.input_dim, args.input_dim),
            latent_dim=args.latent_dim,
            reconstruction_loss=args.loss,
        )

    # Training Config
    training_config = BaseTrainerConfig(
        output_dir=LOG_DIR,
        num_epochs=args.n_epochs,
        learning_rate=args.lr,
        per_device_train_batch_size=batch_size_train,
        per_device_eval_batch_size=batch_size_eval,
        scheduler_cls="ReduceLROnPlateau",
        scheduler_params={"patience": 5, "factor": 0.5}
    )

    # FID score computation class
    fid_calculator = FrechetInceptionDistance(feature=64, reset_real_features=False, normalize=True)
    fid_calculator.update(train_dataset.expand(train_dataset.shape[0], 3, args.input_dim, args.input_dim).to('cpu'), real=True)

    # IS score computation class
    is_calculator = InceptionScore(normalize=True)

    # Create an empty DataFrame to log the FID
    df = pd.DataFrame(columns=['fid', 'is_mean','is_std'])
    df.index.name = 'run'

    # Training loop
    for i in range(args.n_runs):

        print(f"RUN {i}")

        # Init Model
        model = model_dict[args.model](
            model_config=model_config,
            encoder=architecture_dict[args.architecture]['encoder'](model_config), 
            decoder=architecture_dict[args.architecture]['decoder'](model_config) 
        )

        # W & B
        wandb_cb = WandbCallback()
        wandb_cb.setup(
            training_config=training_config, # training config
            model_config=model_config, # model config
            project_name="model-degeneration", # specify your wandb project,
        )

        wandb.run.name = f"experiment_{timestamp}_run_{i}"
        wandb.config.update(args)
        wandb.config.update({'run': i})

        callbacks = []
        callbacks.append(wandb_cb)

        if i==0:
            wandb.log({"Training Data": wandb.Image(train_dataset),
                       "Evaluation Data": wandb.Image(eval_dataset)})
        else:
            wandb.log({"Generated Data": wandb.Image(gen_data)})

        wandb.config.update({'num_params': count_parameters(model)})

        # trainer = BaseTrainer(
        #     model=model,
        #     train_dataset=train_dataset,
        #     eval_dataset=eval_dataset,
        #     training_config=training_config
        # )

        #trainer.train()


        # Train
        pipeline = TrainingPipeline(
            training_config=training_config,
            model=model
        )

        pipeline(
            train_data=train_dataset,
            eval_data=eval_dataset,
            callbacks=callbacks 
        )

        # Generate synthetic data
        if args.sampler == 'normal':
            sampler = NormalSampler(
                sampler_config=None,
                model=model
            )
        elif args.sampler == 'gmm':
            sampler_config = GaussianMixtureSamplerConfig(
                n_components=10
            )

            sampler = GaussianMixtureSampler(
                sampler_config=sampler_config,
                model=model
            )

            sampler.fit(
                train_data=train_dataset
            )
        elif args.sampler == 'rhvae':
            sampler_config = RHVAESamplerConfig(
                mcmc_steps_nbr=100,
                n_lf=10,
                eps_lf=0.03
            )

            sampler = RHVAESampler(
                sampler_config=sampler_config,
                model=model
            )

            sampler.fit(
                train_data=train_dataset
            )

        gen_data = sampler.sample(
            num_samples=args.k,
        )

        # Compute FID score and add to DataFrame
        fid_calculator.update(gen_data.expand(gen_data.shape[0], 3, args.input_dim, args.input_dim).cpu(), real=False)
        fid_score = fid_calculator.compute().item()
        print(fid_score)
        df.loc[i, 'fid'] = fid_score

        # Compute IS score and add to DataFrame
        is_calculator.update(gen_data.expand(gen_data.shape[0], 3, args.input_dim, args.input_dim).cpu())
        is_score = is_calculator.compute()
        df.loc[i, 'is_mean'] = is_score[0].item()
        df.loc[i, 'is_std'] = is_score[1].item()

        # Save DataFrame
        df.to_csv(f'{LOG_DIR}/gendata_metrics.csv')

        # Save Generated data as NumPy array to a file
        np.save(f'{LOG_DIR}/gendata_{i}.npy', gen_data.cpu().numpy())
        gen_data = gen_data.to(device)
        # Update Training Dataset with Generated Data
        #train_dataset = ConcatDataset([train_dataset, gen_data])
        train_dataset = torch.cat((train_dataset, gen_data), 0)
        # shuffle
        train_dataset = train_dataset[torch.randperm(train_dataset.size()[0])]
