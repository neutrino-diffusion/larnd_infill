from abc import ABC, abstractmethod
from collections import namedtuple

import MinkowskiEngine as ME
import torch; import torch.nn as nn; from torch.nn.utils.rnn import pad_sequence
from chamfer_distance import ChamferDistance


def init_loss_func(conf):
    if (
        conf.loss_func == "PixelWise_L1Loss" or
        conf.loss_func == "PixelWise_MSELoss" or
        conf.loss_func == "PixelWise_BCEWithLogitsLoss"
    ):
        loss = PixelWise(conf)
    elif (
        conf.loss_func == "GapWise_L1Loss" or
        conf.loss_func == "GapWise_MSELoss" or
        conf.loss_func == "GapWise_L1Loss_MSELossPixelWise"
    ):
        loss = GapWise(conf)
    elif conf.loss_func == "PlaneWise_L1Loss":
        loss = PlaneWise(conf)
    elif conf.loss_func == "Chamfer":
        loss = Chamfer(conf)
        raise NotImplementedError(
            "Don't think Chamfer loss is possible, would need to use point clouds"
        )
    else:
        raise ValueError("loss_func={} not valid".format(conf.loss_func))

    return loss


class BCE:
    def __init__(self, reduction="mean"):
        self.crit = nn.BCELoss(reduction=reduction)

    def __call__(self, pred, target):
        return self.crit(pred, target)


class CustomLoss(ABC):
    @abstractmethod
    def __init__(self, conf, override_opts={}):
        pass

    @abstractmethod
    def get_names_scalefactors(self):
        pass

    @abstractmethod
    def calc_loss(self, s_pred, s_in, s_target, data):
        pass


class PixelWise(CustomLoss):
    def __init__(self, conf, override_opts={}):
        if override_opts:
            conf_dict = conf._asdict()
            for opt_name, opt_val in override_opts.items():
                conf_dict[opt_name] = opt_val
            conf_namedtuple = namedtuple("config", conf_dict)
            conf = conf_namedtuple(**conf_dict)

        if conf.loss_func == "PixelWise_L1Loss":
            self.crit = nn.L1Loss()
            self.crit_sumreduction = nn.L1Loss(reduction="sum")
        elif conf.loss_func == "PixelWise_MSELoss":
            self.crit = nn.MSELoss()
            self.crit_sumreduction = nn.MSELoss(reduction="sum")
        elif conf.loss_func == "PixelWise_BCEWithLogitsLoss":
            self.crit = nn.BCEWithLogitsLoss()
            self.crit_sumreduction = nn.BCEWithLogitsLoss(reduction="sum")
        else:
            raise NotImplementedError("loss_func={} not valid".format(conf.loss_func))

        self.lambda_loss_infill_zero = getattr(conf, "loss_infill_zero_weight", 0.0)
        self.lambda_loss_infill_nonzero = getattr(conf, "loss_infill_nonzero_weight", 0.0)
        self.lambda_loss_active_zero = getattr(conf, "loss_active_zero_weight", 0.0)
        self.lambda_loss_active_nonzero = getattr(conf, "loss_active_nonzero_weight", 0.0)
        self.lambda_loss_infill = getattr(conf, "loss_infill_weight", 0.0)
        self.lambda_loss_active = getattr(conf, "loss_active_weight", 0.0)
        self.lambda_loss_infill_sum = getattr(conf, "loss_infill_sum_weight", 0.0)

    def get_names_scalefactors(self):
        return {
            "infill_zero" : self.lambda_loss_infill_zero,
            "infill_nonzero" : self.lambda_loss_infill_nonzero,
            "active_zero" : self.lambda_loss_active_zero,
            "active_nonzero" : self.lambda_loss_active_nonzero,
            "infill" : self.lambda_loss_infill,
            "active" : self.lambda_loss_active,
            "infill_sum" : self.lambda_loss_infill_sum
        }

    def calc_loss(self, s_pred, s_in, s_target, data):
        batch_size = len(data["mask_x"])

        (
            infill_coords, active_coords,
            infill_coords_zero, infill_coords_nonzero,
            active_coords_zero, active_coords_nonzero
        ) = self._get_infill_active_coords(s_in, s_target)

        losses_infill_zero, losses_infill_nonzero, losses_infill = [], [], []
        losses_active_zero, losses_active_nonzero, losses_active = [], [], []
        losses_infill_sum = []

        # Compute loss separately for each image and then average over batch
        for i_batch in range(batch_size):
            if self.lambda_loss_infill_zero:
                coords = infill_coords_zero[infill_coords_zero[:, 0] == i_batch]
                losses_infill_zero.append(self._get_loss_at_coords(s_pred, s_target, coords))

            if self.lambda_loss_infill_nonzero:
                coords = infill_coords_nonzero[infill_coords_nonzero[:, 0] == i_batch]
                losses_infill_nonzero.append(self._get_loss_at_coords(s_pred, s_target, coords))

            if self.lambda_loss_infill or self.lambda_loss_infill_sum:
                coords = infill_coords[infill_coords[:, 0] == i_batch]
                if self.lambda_loss_infill:
                    losses_infill.append(self._get_loss_at_coords(s_pred, s_target, coords))
                if self.lambda_loss_infill_sum:
                    losses_infill_sum.append(
                        self._get_summed_loss_at_coords(s_pred, s_target, coords)
                    )

            if self.lambda_loss_active_zero:
                coords = active_coords_zero[active_coords_zero[:, 0] == i_batch]
                losses_active_zero.append(self._get_loss_at_coords(s_pred, s_target, coords))

            if self.lambda_loss_active_nonzero:
                coords = active_coords_nonzero[active_coords_nonzero[:, 0] == i_batch]
                losses_active_nonzero.append(self._get_loss_at_coords(s_pred, s_target, coords))

            if self.lambda_loss_active:
                coords = active_coords[active_coords[:, 0] == i_batch]
                losses_active.append(self._get_loss_at_coords(s_pred, s_target, coords))

        loss_tot = 0.0
        losses = {}
        if self.lambda_loss_infill_zero:
            loss = sum(losses_infill_zero) / batch_size
            loss_tot += self.lambda_loss_infill_zero * loss
            losses["infill_zero"] = loss
        if self.lambda_loss_infill_nonzero:
            loss = sum(losses_infill_nonzero) / batch_size
            loss_tot += self.lambda_loss_infill_nonzero * loss
            losses["infill_nonzero"] = loss
        if self.lambda_loss_infill:
            loss = sum(losses_infill) / batch_size
            loss_tot += self.lambda_loss_infill * loss
            losses["infill"] = loss
        if self.lambda_loss_infill_sum:
            loss = sum(losses_infill_sum) / batch_size
            loss_tot += self.lambda_loss_infill_sum * loss
            losses["infill_sum"] = loss
        if self.lambda_loss_active_zero:
            loss = sum(losses_active_zero) / batch_size
            loss_tot += self.lambda_loss_active_zero * loss
            losses["active_zero"] = loss
        if self.lambda_loss_active_nonzero:
            loss = sum(losses_active_nonzero) / batch_size
            loss_tot += self.lambda_loss_active_nonzero * loss
            losses["active_nonzero"] = loss
        if self.lambda_loss_active:
            loss = sum(losses_active) / batch_size
            loss_tot += self.lambda_loss_active * loss
            losses["active"] = loss

        return loss_tot, losses

    def _get_infill_active_coords(self, s_in, s_target):
        s_in_infill_mask = s_in.F[:, -1] == 1
        infill_coords = s_in.C[s_in_infill_mask].type(torch.float)
        active_coords = s_in.C[~s_in_infill_mask].type(torch.float)

        infill_coords_zero_mask = s_target.features_at_coordinates(infill_coords)[:, 0] == 0
        infill_coords_zero = infill_coords[infill_coords_zero_mask]
        infill_coords_nonzero = infill_coords[~infill_coords_zero_mask]

        active_coords_zero_mask = s_target.features_at_coordinates(active_coords)[:, 0] == 0
        active_coords_zero = active_coords[active_coords_zero_mask]
        active_coords_nonzero = active_coords[~active_coords_zero_mask]

        return (
            infill_coords, active_coords,
            infill_coords_zero, infill_coords_nonzero,
            active_coords_zero, active_coords_nonzero
        )

    def _get_loss_at_coords(self, s_pred, s_target, coords):
        if coords.shape[0]:
            loss = self.crit(
                s_pred.features_at_coordinates(coords).squeeze(),
                s_target.features_at_coordinates(coords).squeeze()
            )
        else:
            loss = self.crit_sumreduction(
                s_pred.features_at_coordinates(coords).squeeze(),
                s_target.features_at_coordinates(coords).squeeze()
            )

        return loss

    def _get_summed_loss_at_coords(self, s_pred, s_target, coords):
        if coords.shape[0]:
            # The denominator bounds the loss [0,1] since voxels are bounded [0,1] by the hardtanh
            loss = self.crit(
                s_pred.features_at_coordinates(coords).squeeze().sum() / coords.shape[0],
                s_target.features_at_coordinates(coords).squeeze().sum() / coords.shape[0]
            )
        else:
            loss = self.crit_sumreduction(
                s_pred.features_at_coordinates(coords).squeeze().sum(),
                s_target.features_at_coordinates(coords).squeeze().sum()
            )

        return loss


class GapWise(CustomLoss):
    """
    Loss on the summed adc + summed active pixels in x or z infill gaps
    """
    def __init__(self, conf, override_opts={}):
        if override_opts:
            conf_dict = conf._asdict()
            for opt_name, opt_val in override_opts.items():
                conf_dict[opt_name] = opt_val
            conf_namedtuple = namedtuple("config", conf_dict)
            conf = conf_namedtuple(**conf_dict)

        if conf.loss_func == "GapWise_L1Loss":
            self.crit_adc = nn.L1Loss()
            self.crit_adc_sumreduction = nn.L1Loss(reduction="sum")
            self.crit_npixel = nn.L1Loss()
            self.crit_adc_pixelwise = nn.L1Loss()
            self.crit_adc_pixelwise_sumreduction = nn.L1Loss(reduction="sum")
        elif conf.loss_func == "GapWise_MSELoss":
            self.crit_adc = nn.MSELoss()
            self.crit_adc_sumreduction = nn.MSELoss(reduction="sum")
            self.crit_npixel = nn.MSELoss()
            self.crit_adc_pixelwise = nn.MSELoss()
            self.crit_adc_pixelwise_sumreduction = nn.MSELoss(reduction="sum")
        elif conf.loss_func == "GapWise_L1Loss_MSELossPixelWise":
            self.crit_adc = nn.L1Loss()
            self.crit_adc_sumreduction = nn.L1Loss(reduction="sum")
            self.crit_npixel = nn.L1Loss()
            self.crit_adc_pixelwise = nn.MSELoss()
            self.crit_adc_pixelwise_sumreduction = nn.MSELoss(reduction="sum")
        # elif conf.loss_func == "GapWise_L1Loss_BCELoss":
        #     self.crit_adc = nn.L1Loss()
        #     self.crit_adc_sumreduction = nn.L1Loss(reduction="sum")
        #     self.crit_npixel = nn.BCELoss()

        self.adc_threshold = conf.adc_threshold

        self.lambda_loss_infill_zero = getattr(conf, "loss_infill_zero_weight", 0.0)
        self.lambda_loss_infill_nonzero = getattr(conf, "loss_infill_nonzero_weight", 0.0)
        self.lambda_loss_infill = getattr(conf, "loss_infill_weight", 0.0)
        self.lambda_loss_x_gaps_adc = getattr(conf, "loss_x_gaps_adc_weight", 0.0)
        self.lambda_loss_x_gaps_npixel = getattr(conf, "loss_x_gaps_npixel_weight", 0.0)
        self.lambda_loss_z_gaps_adc = getattr(conf, "loss_z_gaps_adc_weight", 0.0)
        self.lambda_loss_z_gaps_npixel = getattr(conf, "loss_z_gaps_npixel_weight", 0.0)

        if (
            self.adc_threshold is None or self.adc_threshold == 0 and
            (self.lambda_loss_x_gaps_npixel or self.lambda_loss_z_gaps_npixel)
        ):
            raise ValueError(
                "Need to have an adc threshold set and >0 for the final pruning layer" +
                "in order to use npixel losses"
            )

    def get_names_scalefactors(self):
        return {
            "infill_zero" : self.lambda_loss_infill_zero,
            "infill_nonzero" : self.lambda_loss_infill_nonzero,
            "infill" : self.lambda_loss_infill,
            "x_gaps_adc" : self.lambda_loss_x_gaps_adc,
            "x_gaps_npixel" : self.lambda_loss_x_gaps_npixel,
            "z_gaps_adc" : self.lambda_loss_z_gaps_adc,
            "z_gaps_npixel" : self.lambda_loss_z_gaps_npixel
        }

    def calc_loss(self, s_pred, s_in, s_target, data):
        batch_size = len(data["mask_x"])

        infill_coords, infill_coords_zero, infill_coords_nonzero = self._get_infill_coords(
            s_in, s_target
        )

        losses_infill_zero, losses_infill_nonzero, losses_infill = [], [] ,[]
        losses_x_gap_adc, losses_x_gap_npixel = [], []
        losses_z_gap_adc, losses_z_gap_npixel = [], []

        # compute selected losses for each image in batch
        for i_batch in range(batch_size):
            if self.lambda_loss_infill_zero:
                batch_infill_coords_zero = (
                    infill_coords_zero[infill_coords_zero[:, 0] == i_batch]
                )
                losses_infill_zero.append(
                    self._get_loss_at_coords(s_pred, s_target, batch_infill_coords_zero)
                )

            if self.lambda_loss_infill_nonzero:
                batch_infill_coords_nonzero = (
                    infill_coords_nonzero[infill_coords_nonzero[:, 0] == i_batch]
                )
                losses_infill_nonzero.append(
                    self._get_loss_at_coords(s_pred, s_target, batch_infill_coords_nonzero)
                )

            batch_infill_coords = infill_coords[infill_coords[:, 0] == i_batch]

            if self.lambda_loss_infill:
                losses_infill.append(
                    self._get_loss_at_coords(s_pred, s_target, batch_infill_coords)
                )

            ret = self._get_gap_losses(
                s_pred, s_target,
                set(data["mask_x"][i_batch]),
                batch_infill_coords,
                1,
                skip_adc=(not self.lambda_loss_x_gaps_adc),
                skip_npixel=(not self.lambda_loss_x_gaps_npixel)
            )
            losses_x_gap_adc.append(ret[0])
            losses_x_gap_npixel.append(ret[1])

            ret = self._get_gap_losses(
                s_pred, s_target,
                set(data["mask_z"][i_batch]),
                batch_infill_coords,
                3,
                skip_adc=(not self.lambda_loss_z_gaps_adc),
                skip_npixel=(not self.lambda_loss_z_gaps_npixel)
            )
            losses_z_gap_adc.append(ret[0])
            losses_z_gap_npixel.append(ret[1])

        # average each loss over batch and calculate total loss
        loss_tot = 0.0
        losses = {}
        if self.lambda_loss_infill_zero:
            loss = sum(losses_infill_zero) / batch_size
            loss_tot += self.lambda_loss_infill_zero * loss
            losses["infill_zero"] = loss
        if self.lambda_loss_infill_nonzero:
            loss = sum(losses_infill_nonzero) / batch_size
            loss_tot += self.lambda_loss_infill_nonzero * loss
            losses["infill_nonzero"] = loss
        if self.lambda_loss_infill:
            loss = sum(losses_infill) / batch_size
            loss_tot += self.lambda_loss_infill * loss
            losses["infill"] = loss
        if self.lambda_loss_x_gaps_adc:
            loss = sum(losses_x_gap_adc) / batch_size
            loss_tot += self.lambda_loss_x_gaps_adc * loss
            losses["x_gaps_adc"] = loss
        if self.lambda_loss_x_gaps_npixel:
            loss = sum(losses_x_gap_npixel) / batch_size
            loss_tot += self.lambda_loss_x_gaps_npixel * loss
            losses["x_gaps_npixel"] = loss
        if self.lambda_loss_z_gaps_adc:
            loss = sum(losses_z_gap_adc) / batch_size
            loss_tot += self.lambda_loss_z_gaps_adc * loss
            losses["z_gaps_adc"] = loss
        if self.lambda_loss_z_gaps_npixel:
            loss = sum(losses_z_gap_npixel) / batch_size
            loss_tot += self.lambda_loss_z_gaps_npixel * loss
            losses["z_gaps_npixel"] = loss

        return loss_tot, losses

    def _get_infill_coords(self, s_in, s_target):
        s_in_infill_mask = s_in.F[:, -1] == 1
        infill_coords = s_in.C[s_in_infill_mask].type(torch.float)

        infill_coords_zero_mask = s_target.features_at_coordinates(infill_coords)[:, 0] == 0
        infill_coords_zero = infill_coords[infill_coords_zero_mask]
        infill_coords_nonzero = infill_coords[~infill_coords_zero_mask]

        return infill_coords, infill_coords_zero, infill_coords_nonzero

    def _get_loss_at_coords(self, s_pred, s_target, coords):
        if coords.shape[0]:
            loss = self.crit_adc_pixelwise(
                s_pred.features_at_coordinates(coords).squeeze(),
                s_target.features_at_coordinates(coords).squeeze()
            )
        else:
            loss = self.crit_adc_pixelwise_sumreduction(
                s_pred.features_at_coordinates(coords).squeeze(),
                s_target.features_at_coordinates(coords).squeeze()
            )

        return loss

    def _get_gap_losses(
        self, s_pred, s_target, gaps, infill_coords, coord_idx, skip_adc=False, skip_npixel=False
    ):
        if skip_adc and skip_npixel:
            return 0, 0

        gap_losses_adc, gap_losses_npixel = [], []

        # Only considering gap coords (x or z) that contain active coords in the plane
        active_gap_coords = [
            int(gap_coord)
            for gap_coord in torch.unique(infill_coords[:, coord_idx]).tolist()
                if int(gap_coord) in gaps
        ]
        # No active coords in any gaps,
        # use a token gap coord so we still have something to backprop in the end
        # XXX Just return zero here and hope there is a pixel-wise loss with a non-zero weight in
        # the sum
        if not active_gap_coords:
            # active_gap_coords = [next(iter(gaps))]
            return torch.tensor(0.0), torch.tensor(0.0)
        gap_ranges = self._get_edge_ranges(active_gap_coords)

        for gap_start, gap_end in gap_ranges:
            gap_mask = sum(
                infill_coords[:, coord_idx] == gap_coord
                for gap_coord in range(gap_start, gap_end + 1)
            )
            gap_coords = infill_coords[gap_mask.type(torch.bool)]

            if not skip_adc:
                gap_losses_adc.append(
                    self.crit_adc(
                        (
                            s_pred.features_at_coordinates(gap_coords).squeeze().sum() /
                            gap_coords.shape[0]
                        ),
                        (
                            s_target.features_at_coordinates(gap_coords).squeeze().sum() /
                            gap_coords.shape[0]
                        )
                    )
                )

            if not skip_npixel:
                gap_losses_npixel.append(
                    self.crit_npixel(
                        (
                            torch.clamp(
                                s_pred.features_at_coordinates(gap_coords).squeeze(),
                                min=0.0, max=self.adc_threshold
                            ).sum(dtype=s_pred.F.dtype) /
                            self.adc_threshold /
                            gap_coords.shape[0]
                        ),
                        (
                            torch.clamp(
                                s_target.features_at_coordinates(gap_coords).squeeze(),
                                min=0.0, max=self.adc_threshold
                            ).sum(dtype=s_target.F.dtype) /
                            self.adc_threshold /
                            gap_coords.shape[0]
                        )
                    )
                )

        gap_loss_adc =  sum(gap_losses_adc) / len(gap_losses_adc) if not skip_adc else 0
        gap_loss_npixel = sum(gap_losses_npixel) / len(gap_losses_npixel) if not skip_npixel else 0

        return gap_loss_adc, gap_loss_npixel

    @staticmethod
    def _get_edge_ranges(nums):
        nums = sorted(set(nums))
        discontinuities = [[s, e] for s, e in zip(nums, nums[1:]) if s+1 < e]
        edges = iter(nums[:1] + sum(discontinuities, []) + nums[-1:])
        return list(zip(edges, edges))

class PlaneWise(CustomLoss):
    """
    loss on the summed adc + summed active pixels in planes of x or z = const in infill regions
    """
    def __init__(self, conf, override_opts={}):
        if override_opts:
            conf_dict = conf._asdict()
            for opt_name, opt_val in override_opts.items():
                conf_dict[opt_name] = opt_val
            conf_namedtuple = namedtuple("config", conf_dict)
            conf = conf_namedtuple(**conf_dict)

        if conf.loss_func == "PlaneWise_L1Loss":
            self.crit_adc = nn.L1Loss()
            self.crit_adc_sumreduction = nn.L1Loss(reduction="sum")
            self.crit_npixel = nn.L1Loss()

        self.adc_threshold = conf.adc_threshold

        self.lambda_loss_infill_zero = getattr(conf, "loss_infill_zero_weight", 0.0)
        self.lambda_loss_infill_nonzero = getattr(conf, "loss_infill_nonzero_weight", 0.0)
        self.lambda_loss_infill = getattr(conf, "loss_infill_weight", 0.0)
        self.lambda_loss_x_planes_adc = getattr(conf, "loss_x_planes_adc_weight", 0.0)
        self.lambda_loss_x_planes_npixel = getattr(conf, "loss_x_planes_npixel_weight", 0.0)
        self.lambda_loss_z_planes_adc = getattr(conf, "loss_z_planes_adc_weight", 0.0)
        self.lambda_loss_z_planes_npixel = getattr(conf, "loss_z_planes_npixel_weight", 0.0)

        if (
            self.adc_threshold is None and
            (self.lambda_loss_x_planes_npixel or self.lambda_loss_z_planes_npixel)
        ):
            raise ValueError(
                "Need to have an adc threshold for the final pruning layer" +
                "in order to use npixel losses"
            )

    def get_names_scalefactors(self):
        return {
            "infill_zero" : self.lambda_loss_infill_zero,
            "infill_nonzero" : self.lambda_loss_infill_nonzero,
            "infill" : self.lambda_loss_infill,
            "x_planes_adc" : self.lambda_loss_x_planes_adc,
            "x_planes_npixel" : self.lambda_loss_x_planes_npixel,
            "z_planes_adc" : self.lambda_loss_z_planes_adc,
            "z_planes_npixel" : self.lambda_loss_z_planes_npixel
        }

    def calc_loss(self, s_pred, s_in, s_target, data):
        batch_size = len(data["mask_x"])

        infill_coords, infill_coords_zero, infill_coords_nonzero = self._get_infill_coords(
            s_in, s_target
        )

        losses_infill_zero, losses_infill_nonzero, losses_infill = [], [] ,[]
        losses_x_plane_adc, losses_x_plane_npixel = [], []
        losses_z_plane_adc, losses_z_plane_npixel = [], []

        # compute selected losses for each image in batch
        for i_batch in range(batch_size):
            if self.lambda_loss_infill_zero:
                batch_infill_coords_zero = (
                    infill_coords_zero[infill_coords_zero[:, 0] == i_batch]
                )
                losses_infill_zero.append(
                    self._get_loss_at_coords(s_pred, s_target, batch_infill_coords_zero)
                )

            if self.lambda_loss_infill_nonzero:
                batch_infill_coords_nonzero = (
                    infill_coords_nonzero[infill_coords_nonzero[:, 0] == i_batch]
                )
                losses_infill_nonzero.append(
                    self._get_loss_at_coords(s_pred, s_target, batch_infill_coords_nonzero)
                )

            batch_infill_coords = infill_coords[infill_coords[:, 0] == i_batch]

            if self.lambda_loss_infill:
                losses_infill.append(
                    self._get_loss_at_coords(s_pred, s_target, batch_infill_coords)
                )

            ret = self._get_plane_losses(
                s_pred, s_target,
                set(data["mask_x"][i_batch]),
                batch_infill_coords,
                1,
                skip_adc=(not self.lambda_loss_x_planes_adc),
                skip_npixel=(not self.lambda_loss_x_planes_npixel)
            )
            losses_x_plane_adc.append(ret[0])
            losses_x_plane_npixel.append(ret[1])

            ret = self._get_plane_losses(
                s_pred, s_target,
                set(data["mask_z"][i_batch]),
                batch_infill_coords,
                3,
                skip_adc=(not self.lambda_loss_z_planes_adc),
                skip_npixel=(not self.lambda_loss_z_planes_npixel)
            )
            losses_z_plane_adc.append(ret[0])
            losses_z_plane_npixel.append(ret[1])

        # average each loss over batch and calculate total loss
        loss_tot = 0.0
        losses = {}
        if self.lambda_loss_infill_zero:
            loss = sum(losses_infill_zero) / batch_size
            loss_tot += self.lambda_loss_infill_zero * loss
            losses["infill_zero"] = loss
        if self.lambda_loss_infill_nonzero:
            loss = sum(losses_infill_nonzero) / batch_size
            loss_tot += self.lambda_loss_infill_nonzero * loss
            losses["infill_nonzero"] = loss
        if self.lambda_loss_infill:
            loss = sum(losses_infill) / batch_size
            loss_tot += self.lambda_loss_infill * loss
            losses["infill"] = loss
        if self.lambda_loss_x_planes_adc:
            loss = sum(losses_x_plane_adc) / batch_size
            loss_tot += self.lambda_loss_x_planes_adc * loss
            losses["x_planes_adc"] = loss
        if self.lambda_loss_x_planes_npixel:
            loss = sum(losses_x_plane_npixel) / batch_size
            loss_tot += self.lambda_loss_x_planes_npixel * loss
            losses["x_planes_npixel"] = loss
        if self.lambda_loss_z_planes_adc:
            loss = sum(losses_z_plane_adc) / batch_size
            loss_tot += self.lambda_loss_z_planes_adc * loss
            losses["z_planes_adc"] = loss
        if self.lambda_loss_z_planes_npixel:
            loss = sum(losses_z_plane_npixel) / batch_size
            loss_tot += self.lambda_loss_z_planes_npixel * loss
            losses["z_planes_npixel"] = loss

        return loss_tot, losses

    def _get_infill_coords(self, s_in, s_target):
        s_in_infill_mask = s_in.F[:, -1] == 1
        infill_coords = s_in.C[s_in_infill_mask].type(torch.float)

        infill_coords_zero_mask = s_target.features_at_coordinates(infill_coords)[:, 0] == 0
        infill_coords_zero = infill_coords[infill_coords_zero_mask]
        infill_coords_nonzero = infill_coords[~infill_coords_zero_mask]

        return infill_coords, infill_coords_zero, infill_coords_nonzero

    def _get_loss_at_coords(self, s_pred, s_target, coords):
        if coords.shape[0]:
            loss = self.crit_adc(
                s_pred.features_at_coordinates(coords).squeeze(),
                s_target.features_at_coordinates(coords).squeeze()
            )
        else:
            loss = self.crit_adc_sumreduction(
                s_pred.features_at_coordinates(coords).squeeze(),
                s_target.features_at_coordinates(coords).squeeze()
            )

        return loss

    def _get_plane_losses(
        self, s_pred, s_target, gaps, infill_coords, coord_idx, skip_adc=False, skip_npixel=False
    ):
        if skip_adc and skip_npixel:
            return 0, 0

        plane_losses_adc, plane_losses_npixel = [], []

        for gap_coord in gaps:
            plane_coords = infill_coords[infill_coords[:, coord_idx] == gap_coord]

            if not skip_adc:
                plane_losses_adc.append(
                    self.crit_adc(
                        s_pred.features_at_coordinates(plane_coords).squeeze().sum(),
                        s_target.features_at_coordinates(plane_coords).squeeze().sum()
                    )
                )

            # Way to allow gradients for this counting of active pixels.
            if not skip_npixel:
                plane_losses_npixel.append(
                    self.crit_npixel(
                        (
                            torch.clamp(
                                s_pred.features_at_coordinates(plane_coords).squeeze(),
                                min=0.0, max=self.adc_threshold
                            ).sum(dtype=s_pred.F.dtype) /
                            self.adc_threshold
                        ),
                        (
                            torch.clamp(
                                s_target.features_at_coordinates(plane_coords).squeeze(),
                                min=0.0, max=self.adc_threshold
                            ).sum(dtype=s_target.F.dtype) /
                            self.adc_threshold
                        )
                    )
                )

        plane_loss_adc = (
            sum(plane_losses_adc) / len(plane_losses_adc) if not skip_adc else 0
        )
        plane_loss_npixel = (
            sum(plane_losses_npixel) / len(plane_losses_npixel) if not skip_npixel else 0
        )

        return plane_loss_adc, plane_loss_npixel

# NOTE I dont know how to get this working, in current state seems to compute Chamfer distance
# correctly but there are no gradients making it through. Need a way to use the features of the
# sparse tensors since these are what have the gradients
class Chamfer(CustomLoss):
    """
    Loss based around Chamfer loss for pointclouds
    """
    def __init__(self, conf, override_opts={}):
        if override_opts:
            conf_dict = conf._asdict()
            for opt_name, opt_val in override_opts.items():
                conf_dict[opt_name] = opt_val
            conf_namedtuple = namedtuple("config", conf_dict)
            conf = conf_namedtuple(**conf_dict)

        self.chamfer_distance = ChamferDistance()

        self.device = torch.device(conf.device)

        self.lambda_loss_infill_chamfer = getattr(conf, "loss_infill_chamfer_weight", 0.0)

    def get_names_scalefactors(self):
        return { "infill_chamfer" : self.lambda_loss_infill_chamfer }

    def calc_loss(self, s_pred, s_in, s_target, data):
        loss_tot = 0.0
        losses = {}

        infill_coords_nonzero_pred, infill_coords_nonzero_target = self._get_infill_coords(
            s_in, s_target, s_pred
        )

        x = [
            infill_coords_nonzero_pred[infill_coords_nonzero_pred[:,0] == i_batch][:,1:].type(torch.float)
            for i_batch in range(len(data["mask_x"]))
        ]
        x_lengths = torch.tensor([ b.shape[0] for b in x ], device=self.device, dtype=torch.long)
        x = pad_sequence(x, batch_first=True)
        y = [
            infill_coords_nonzero_target[infill_coords_nonzero_target[:,0] == i_batch][:,1:].type(torch.float)
            for i_batch in range(len(data["mask_x"]))
        ]
        y_lengths = torch.tensor([ b.shape[0] for b in y ], device=self.device, dtype=torch.long)
        y = pad_sequence(y, batch_first=True)

        # print(x)
        # print(x.shape)
        # print(y)
        # print(y.shape)
        cham_dist_1, cham_dist_2, _, _ = self.chamfer_distance(
            x, y, x_lengths=x_lengths, y_lengths=y_lengths
        )
        # print(cham_dist_1)
        # print(cham_dist_1.shape)
        # print(cham_dist_2)
        # print(cham_dist_2.shape)
        loss_infill_chamfer = torch.mean(cham_dist_1) + torch.mean(cham_dist_2)
        # print(loss_infill_chamfer)
        loss_tot += self.lambda_loss_infill_chamfer * loss_infill_chamfer
        losses["infill_chamfer"] = loss_infill_chamfer

        return loss_tot, losses

    def _get_infill_coords(self, s_in, s_target, s_pred):
        s_in_infill_mask = s_in.F[:, -1] == 1
        infill_coords = s_in.C[s_in_infill_mask]
        infill_coords_float = infill_coords.type(torch.float)

        infill_coords_nonzero_target_mask = (
            s_target.features_at_coordinates(infill_coords_float)[:, 0] != 0
        )
        infill_coords_nonzero_target = infill_coords[infill_coords_nonzero_target_mask]

        infill_coords_nonzero_pred_mask = (
            s_pred.features_at_coordinates(infill_coords_float)[:, 0] != 0
        )
        infill_coords_nonzero_pred = infill_coords[infill_coords_nonzero_pred_mask]

        return infill_coords_nonzero_pred, infill_coords_nonzero_target

