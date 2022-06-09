from typing import List, Optional, Tuple
import random

import torch
from torchvision.utils import make_grid
from sklearn.manifold import MDS
from gtda.diagrams import BettiCurve
from html2image import Html2Image
from captum.attr import visualization
import numpy as np
import matplotlib.pyplot as plt
from captum.attr import LayerAttribution
from gtda.plotting import plot_diagram, plot_betti_surfaces

from gdeep.trainer import Trainer
from gdeep.analysis.interpretability import Interpreter
from gdeep.utility import DEVICE
from ..models import ModelExtractor
from . import (
    persistence_diagrams_of_activations,
    plotly2tensor,
    Compactification,
    png2tensor,
)

FONT_SIZE = 16
Tensor = torch.Tensor


class Visualiser:
    """This class is the bridge to send to the tensorboard
    the required plots. It converts plots and images of
    different kinds to tensors

    Args:
        pipe :
            the pipeline to get info from

    """

    def __init__(self, pipe: Trainer):
        self.pipe = pipe
        self.persistence_diagrams = None

    def plot_data_model(self):
        """This function has no arguments: its purpose
        is to store data to the tensorboard for
        visualisation.
        """

        dataiter = iter(self.pipe.dataloaders[0])
        images, labels = next(dataiter)
        # print(str(labels.item()))
        self.pipe.writer.add_graph(self.pipe.model, images.to(DEVICE))
        features_list = []
        labels_list = []
        index = 0

        for img, lab in dataiter:
            index += 1
            features_list.append(img[0])
            try:
                labels_list.append(str(lab[0].item()))
            except ValueError:
                labels_list.append("Label not available")
            if index == 1000:
                break
        max_number = min(1000, len(labels_list))
        features = torch.cat(features_list).to(DEVICE)
        if len(features.shape) >= 3:
            if len(features.shape) == 3:
                features = features.reshape(
                    max_number, -1, features.shape[1], features.shape[2]
                )
                grid = make_grid(features)
            else:
                grid = make_grid(features)

            self.pipe.writer.add_image("dataset", grid, 0)

            self.pipe.writer.add_embedding(
                features.view(max_number, -1),
                metadata=labels_list,
                label_img=features,
                tag="dataset",
                global_step=0,
            )
        else:
            self.pipe.writer.add_embedding(
                features.view(max_number, -1),
                metadata=labels_list,
                tag="dataset",
                global_step=0,
            )

        self.pipe.writer.flush()

    def plot_activations(self, example=None):
        """Plot PCA of the activations of all layers of the
        `self.model`
        """
        me = ModelExtractor(self.pipe.model, self.pipe.loss_fn)
        inputs = []
        labels = []
        for j, item in enumerate(self.pipe.dataloaders[0]):
            labels.append(item[1][0])
            inputs.append(item[0][0])
            if j == 100:
                break
        inputs = torch.stack(inputs)

        if example is not None:
            inputs = inputs.to(example.dtype)
        acts = me.get_activations(inputs)
        print("Sending the plots to tensorboard: ")
        for i, act in enumerate(acts):
            print("Step " + str(i + 1) + "/" + str(len(acts)), end="\r")
            length = act.shape[0]
            self.pipe.writer.add_embedding(
                act.view(length, -1),
                metadata=labels,
                tag="activations_" + str(i),
                global_step=0,
            )
            self.pipe.writer.flush()

    def plot_persistence_diagrams(self, example=None):
        """Plot a persistence diagrams of the activations
        """
        me = ModelExtractor(self.pipe.model, self.pipe.loss_fn)
        if self.persistence_diagrams is None:
            inputs = []
            for j, item in enumerate(self.pipe.dataloaders[0]):
                inputs.append(item[0][0])
                if j == 100:
                    break
            inputs = torch.stack(inputs)
            if example is not None:
                inputs = inputs.to(example.dtype)
            activ = me.get_activations(inputs)
            self.persistence_diagrams = persistence_diagrams_of_activations(activ)
        list_of_dgms = []
        for i, persistence_diagram in enumerate(self.persistence_diagrams):
            plot_persistence_diagram = plot_diagram(persistence_diagram)
            img_t = plotly2tensor(plot_persistence_diagram)
            list_of_dgms.append(img_t)
        features = torch.stack(list_of_dgms)
        # grid = make_grid(features)
        self.pipe.writer.add_images(
            "persistence_diagrams_of_activations", features, dataformats="NHWC"
        )
        self.pipe.writer.flush()

    def plot_decision_boundary(self, compact=False):
        """Plot the decision boundary as the intrinsic
        hypersurface defined by self.loss_fn == 0.5

        Args:
            compact (bool):
                if plotting the compactified
                version of the boundary
        """

        me = ModelExtractor(self.pipe.model, self.pipe.loss_fn)
        x = next(iter(self.pipe.dataloaders[0]))[0][0]

        if compact:
            # initlaisation of the compactification
            cc = Compactification(
                neural_net=self.pipe.model,
                precision=0.1,
                n_samples=500,
                epsilon=0.051,
                n_features=x.shape[0],
                n_epochs=100,
            )

            d_final, label_final = cc.create_final_distance_matrix()
            embedding = MDS(n_components=3, dissimilarity="precomputed")
            db = embedding.fit_transform(d_final)
            self.pipe.writer.add_embedding(
                db, tag="compactified_decision_boundary", global_step=0
            )
            self.pipe.writer.flush()
            return db, d_final, label_final
        else:
            db = me.get_decision_boundary(x)
            self.pipe.writer.add_embedding(db, tag="decision_boundary", global_step=0)
            self.pipe.writer.flush()
            return db.cpu(), None, None

    def betti_plot_layers(
        self, homology_dimension: Optional[List[int]] = None, example: Optional[Tensor] = None
    ) -> None:
        """
        Args:
            homology_dimension :
                A list of
                homology dimensions, e.g. ``[0, 1, ...]``
            example :
                the selected batch of data to
                compute the activations on. By
                defaults, this method takes the
                first 100 elements

        Returns:
           (None):
                a tuple of figures representing the Betti surfaces
                of the data across layers of the NN, with one figure
                per dimension in homology_dimensions. Otherwise, a single
                figure representing the Betti curve of the single sample
                present.
        """
        if homology_dimension is not None:
            homology_dimension = [0, 1]
        if self.persistence_diagrams is None:
            me = ModelExtractor(self.pipe.model, self.pipe.loss_fn)
            inputs = self.pipe.dataloaders[0].dataset.data[:100]  # type: ignore
            if example is not None:
                inputs = inputs.to(example.dtype)
            self.persistence_diagrams = persistence_diagrams_of_activations(
                me.get_activations(inputs)
            )
        bc = BettiCurve()
        dgms = bc.fit_transform(self.persistence_diagrams)

        plots = plot_betti_surfaces(
            dgms, samplings=bc.samplings_, homology_dimensions=homology_dimension
        )

        for i in range(len(plots)):
            plots[i].show()

    def plot_interpreter_text(self, interpreter: Interpreter):
        """This method allows to plot the results of an
        Interpreter for text data.

        Args:
            interpreter :
                this is a ``gdeep.analysis.interpretability``
                initilised ``Interpreter`` class
                
        Returns:
            matplotlib.figure
        """
        viz = interpreter.stored_visualisations
        fig = visualization.visualize_text(viz)
        name = "out.png"
        hti = Html2Image()
        hti.screenshot(html_str=fig.data, save_as=name)
        img_ten = png2tensor(name)
        self.pipe.writer.add_image(interpreter.method, img_ten, dataformats="HWC")  # type: ignore
        return fig

    def plot_interpreter_image(self, interpreter: Interpreter):
        """This method allows to plot the results of an
        Interpreter for image data.

        Args:
            interpreter:
                this is a ``gdeep.analysis.interpretability``
                initilised ``Interpreter`` class
                
        Returns:
            matplotlib.figure
        """

        try:
            attrib = (
                torch.permute(interpreter.attribution.squeeze().detach(), (1, 2, 0))
                .detach()
                .cpu()
                .numpy()
            )
        except ValueError:
            attrib = torch.permute(
                LayerAttribution.interpolate(
                    interpreter.attribution.detach().cpu(),
                    tuple(interpreter.x.squeeze().detach().cpu().shape[-2:]),
                ).squeeze(0),
                (1, 2, 0),
            )
            attrib = (
                torch.stack([attrib, attrib, attrib], dim=2)
                .squeeze(-1)
                .detach()
                .cpu()
                .numpy()
            )
        img = (
            torch.permute(interpreter.x.squeeze().detach(), (1, 2, 0))
            .detach()
            .cpu()
            .numpy()
        )

        fig, _ = visualization.visualize_image_attr_multiple(
            attrib,
            img,
            ["original_image", "heat_map", "blended_heat_map"],
            ["all", "all", "all"],
            show_colorbar=True,
        )
        self.pipe.writer.add_figure(interpreter.method, fig)  # type: ignore
        return fig

    def plot_feature_importance(self, interpreter: Interpreter):
        """This method allows to plot the results of an
        Interpreter for tabular data.

        Args:
            interpreter :
                this is a ``gdeep.analysis.interpretability``
                initialised ``Interpreter`` class
                
        Returns:
            matplotlib.figure
        """
        # prepare attributions for visualization
        x_test = interpreter.x
        x_axis_data = np.arange(x_test.shape[1])
        x_axis_data_labels = list(map(lambda idx: idx, x_axis_data))

        width = 0.14
        legends = interpreter.features_list

        fig = plt.figure(figsize=(20, 10))

        ax = plt.subplot()
        ax.set_title(
            "Comparing input feature importance across "
            + "multiple algorithms and learned weights"
        )
        ax.set_ylabel("Attributions")

        plt.rc("font", size=FONT_SIZE)  # fontsize of the text sizes
        plt.rc("axes", titlesize=FONT_SIZE)  # fontsize of the axes title
        plt.rc("axes", labelsize=FONT_SIZE)  # fontsize of the x and y labels
        plt.rc("legend", fontsize=FONT_SIZE - 4)  # fontsize of the legend
        for i in range(len(interpreter.attribution_list)):
            attribution_sum = interpreter.attribution_list[i].detach().cpu().numpy().sum(0)
            attribution_norm_sum = attribution_sum / np.linalg.norm(attribution_sum, ord=1)
            r = lambda: random.randint(0, 255)  # noqa
            color = ('#%02X%02X%02X' % (r(), r(), r()))
            ax.bar(
                x_axis_data,
                attribution_norm_sum,
                width,
                align="center",
                alpha=0.8,
                color=color,
            )

        ax.autoscale_view()
        plt.tight_layout()

        ax.set_xticks(x_axis_data + 0.5)
        ax.set_xticklabels(x_axis_data_labels)

        plt.legend(legends, loc=3)
        self.pipe.writer.add_figure("Feature Importance", fig)  # type: ignore
        return plt

    def plot_attribution(self, interpreter: Interpreter, **kwargs):
        """this method geerically plots the attribution of
        the interpreter

        Args:
            interpreter :
                this is a ``gdeep.analysis.interpretability``
                initilised ``Interpreter`` class
            kwargs:
                keywords arguments for the plot (visualize_image_attr of
                captum)

        Returns:
            matplotlib.figure, matplotlib.figure"""
        t1 = Visualiser._adjust_tensors_to_plot(interpreter.x)
        t2 = Visualiser._adjust_tensors_to_plot(interpreter.attribution)
        fig1, _ = visualization.visualize_image_attr(t1, **kwargs)
        fig2, _ = visualization.visualize_image_attr(t2, **kwargs)
        self.pipe.writer.add_figure("Datum x", fig1)  # type: ignore
        self.pipe.writer.add_figure("Generic attribution of x", fig2)  # type: ignore
        return fig1, fig2

    @staticmethod
    def _adjust_tensors_to_plot(tensor: Tensor) -> np.ndarray:
        """make sure that tensors that will be plotted as images have the
        correct dimensions"""
        if len(tensor.shape) == 4:
            tensor = tensor[0, :, :, :]
        elif len(tensor.shape) > 4:
            tensor = tensor[0, :, :, :, 0]
        list_of_permutation = torch.argsort(torch.tensor(tensor.shape)).tolist()
        list_of_permutation.reverse()
        tensor = tensor.permute(*list_of_permutation)
        # if tensor.shape[-1] == 1:
        #     tensor = tensor.squeeze(-1)
        if tensor.shape[-1] == 2:
            temporary = torch.zeros((tensor.shape[0], tensor.shape[1], 3))
            temporary[:, :, :2] = tensor
            tensor = temporary
        else:
            tensor = tensor[:, :, :4]
        return tensor.permute(1, 0, 2).cpu().detach().numpy()
