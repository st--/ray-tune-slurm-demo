# Originally based on
# https://docs.ray.io/en/master/tune/getting-started.html#tune-tutorial

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click
import optuna
import ray
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from ray import tune
from ray.air import CheckpointConfig, RunConfig
from ray.air.integrations.wandb import WandbLoggerCallback
from ray.tune.schedulers import ASHAScheduler
from ray.tune.search.optuna import OptunaSearch
from ray.util.joblib import register_ray
from torch.utils.data import DataLoader
from torchvision import datasets, transforms


class ConvNet(nn.Module):
    def __init__(self, conf_out_channels=3):
        super().__init__()
        self.conv1 = nn.Conv2d(1, conf_out_channels, kernel_size=3)
        self.fc = nn.Linear(64 * conf_out_channels, 10)
        self.conf_out_channels = conf_out_channels

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 3))
        x = x.view(-1, 64 * self.conf_out_channels)
        x = self.fc(x)
        return F.log_softmax(x, dim=1)


def train(
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    train_loader: DataLoader,
) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.train()
    for _batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()


def test(model: nn.Module, data_loader: DataLoader, *, test_size=256) -> float:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for batch_idx, (data, target) in enumerate(data_loader):
            # We set this just for the example to run quickly.
            if batch_idx * len(data) > test_size:
                break
            data, target = data.to(device), target.to(device)
            outputs = model(data)
            _, predicted = torch.max(outputs.data, 1)
            total += target.size(0)
            correct += (predicted == target).sum().item()

    return correct / total


class Trainable(tune.Trainable):
    def setup(self, config):
        mnist_transforms = transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
        )

        data_dir = Path("~/data").expanduser()
        self.train_loader = DataLoader(
            datasets.MNIST(
                str(data_dir), train=True, download=True, transform=mnist_transforms
            ),
            batch_size=64,
            shuffle=True,
        )
        self.test_loader = DataLoader(
            datasets.MNIST(str(data_dir), train=False, transform=mnist_transforms),
            batch_size=64,
            shuffle=True,
        )

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.model = ConvNet(conf_out_channels=config.get("conf_out_channels", 3)).to(
            device
        )

        self.optimizer = optim.SGD(
            self.model.parameters(), lr=config["lr"], momentum=config["momentum"]
        )

    def step(self):
        train(self.model, self.optimizer, self.train_loader)
        acc = test(self.model, self.test_loader)

        # Send the current training result back to Tune
        return dict(mean_accuracy=acc)

    def save_checkpoint(self, checkpoint_dir):
        path = Path(checkpoint_dir) / "checkpoint.pth"
        torch.save(self.model.state_dict(), path)
        return checkpoint_dir

    def load_checkpoint(self, checkpoint_path):
        self.model.load_state_dict(torch.load(checkpoint_path))


def suggest_config(trial: optuna.Trial) -> dict[str, Any]:
    trial.suggest_float("lr", 1e-10, 1e-1, log=True)
    trial.suggest_float("momentum", 0.1, 0.9)
    trial.suggest_categorical("conf_out_channels", [3, 6, 9])
    return {}


@click.command()
@click.option(
    "--tune",
    "do_tune",
    help="Run Tune experiments",
    is_flag=True,
    show_default=True,
    default=False,
)
@click.option("--gpu", is_flag=True, default=False)
@click.option(
    "--restore",
    help="Restore previous training state from this directory",
    default=None,
)
def main(do_tune=False, gpu=False, restore=None):
    study_name = "ray-tune-slurm-test"
    if "redis_password" in os.environ:
        # We're running distributed
        print("ip head: ", os.environ["ip_head"])
        print("redis pwd: ", os.environ["redis_password"])
        _node_ip_addr = os.environ["ip_head"].split(":")[0]
        print("node ip addr: ", _node_ip_addr)
        ray.init(
            address=os.environ["ip_head"],
            _redis_password=os.environ["redis_password"],
            _node_ip_address=_node_ip_addr,
        )
        register_ray()

    # Download the dataset first
    datasets.MNIST("~/data", train=True, download=True)

    run_config = RunConfig(
        callbacks=[
            WandbLoggerCallback(project=study_name),
        ],
        sync_config=ray.train.SyncConfig(),
        stop={"training_iteration": 5},
        checkpoint_config=CheckpointConfig(checkpoint_at_end=True),
        name=study_name,
    )

    train_config = dict(
        lr=1e-10,
        momentum=0.5,
        conf_out_channels=9,
    )

    if do_tune:
        optuna_search = OptunaSearch(
            suggest_config,
            metric="mean_accuracy",
            mode="max",
        )
        if restore:
            print(f"Restoring previous state from {restore}")
            optuna_search.restore_from_dir(restore)

        tuner = tune.Tuner(
            tune.with_resources(Trainable, {"gpu": 1 if gpu else 0, "cpu": 1}),
            tune_config=tune.TuneConfig(
                scheduler=ASHAScheduler(metric="mean_accuracy", mode="max"),
                num_samples=30,
                search_alg=optuna_search,
            ),
            run_config=run_config,
        )
        tuner.fit()
    else:
        tuner = tune.Tuner(
            Trainable,
            param_space=train_config,
            run_config=run_config,
        )
        tuner.fit()


if __name__ == "__main__":
    main()
