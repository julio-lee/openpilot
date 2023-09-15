import math
import numpy as np
from cereal import log
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.numpy_fast import interp
from openpilot.common.realtime import DT_MDL
from openpilot.system.swaglog import cloudlog


TRAJECTORY_SIZE = 33
# camera offset is meters from center car to camera
# model path is in the frame of the camera
PATH_OFFSET = 0.00
CAMERA_OFFSET = 0.04

KEEP_MIN_DISTANCE_FROM_LANE = 1.3

def clamp(num, min_value, max_value):
  return max(min(num, max_value), min_value)

def sigmoid(x, scale=1, offset=0):
  return (1 / (1 + math.exp(x*scale))) + offset

class LanePlanner:
  def __init__(self):
    self.ll_t = np.zeros((TRAJECTORY_SIZE,))
    self.ll_x = np.zeros((TRAJECTORY_SIZE,))
    self.lll_y = np.zeros((TRAJECTORY_SIZE,))
    self.rll_y = np.zeros((TRAJECTORY_SIZE,))
    self.le_y = np.zeros((TRAJECTORY_SIZE,))
    self.re_y = np.zeros((TRAJECTORY_SIZE,))
    self.lane_width_estimate = FirstOrderFilter(3.2, 9.95, DT_MDL)
    self.lane_width = 3.2
    self.lane_change_multiplier = 1

    self.lll_prob = 0.
    self.rll_prob = 0.
    self.d_prob = 0.

    self.lll_std = 0.
    self.rll_std = 0.

    self.l_lane_change_prob = 0.
    self.r_lane_change_prob = 0.

    self.camera_offset = -CAMERA_OFFSET
    self.path_offset = -PATH_OFFSET

  def parse_model(self, md):
    lane_lines = md.laneLines
    edges = md.roadEdges

    if len(lane_lines) == 4 and len(lane_lines[0].t) == TRAJECTORY_SIZE:
      self.ll_t = (np.array(lane_lines[1].t) + np.array(lane_lines[2].t))/2
      # left and right ll x is the same
      self.ll_x = lane_lines[1].x
      self.lll_y = np.array(lane_lines[1].y) + self.camera_offset
      self.rll_y = np.array(lane_lines[2].y) + self.camera_offset
      self.lll_prob = md.laneLineProbs[1]
      self.rll_prob = md.laneLineProbs[2]
      self.lll_std = md.laneLineStds[1]
      self.rll_std = md.laneLineStds[2]

    if len(edges[0].t) == TRAJECTORY_SIZE:
      self.le_y = np.array(edges[0].y) + md.roadEdgeStds[0] * 0.4
      self.re_y = np.array(edges[1].y) - md.roadEdgeStds[1] * 0.4
    else:
      self.le_y = self.lll_y
      self.re_y = self.rll_y

    desire_state = md.meta.desireState
    if len(desire_state):
      self.l_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeLeft]
      self.r_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeRight]

  def get_d_path(self, v_ego, path_t, path_xyz, vcurv):
    # Reduce reliance on uncertain lanelines
    # only give some credit to the model probabilities, rely more on stds and closeness
    distance = self.rll_y[0] - self.lll_y[0]  # 2.8
    right_ratio = self.rll_y[0] / distance  # 2/2.8 = 0.71 (closer to left example)
    l_prob = (right_ratio         + self.lll_prob * 0.4) * interp(self.lll_std, [0, .8], [1.0, 0.01])
    r_prob = ((1.0 - right_ratio) + self.rll_prob * 0.4) * interp(self.rll_std, [0, .8], [1.0, 0.01])

    total_prob = l_prob + r_prob
    if total_prob < 0.01:
      # we've completely lost lanes, we will just use the path, unfortunately
      # this should be very unlikely (impossible even)
      l_prob = 0
      r_prob = 0
    else:
      l_prob = l_prob / total_prob             #normalize to 1
      r_prob = r_prob / total_prob

    # Find current lanewidth
    current_lane_width = clamp(abs(min(self.rll_y[0], self.re_y[0]) - max(self.lll_y[0], self.le_y[0])), 2.6, 4.0)
    self.lane_width_estimate.update(current_lane_width)
    self.lane_width = min(self.lane_width_estimate.x, current_lane_width)

    # ideally we are half distance of lane width
    # but clamp lane distances to not push us over the current lane width
    lane_distance = self.lane_width * 0.5
    use_min_distance = min(lane_distance, KEEP_MIN_DISTANCE_FROM_LANE)
    prepare_wiggle_room = lane_distance - use_min_distance
    curve_prepare = clamp(0.7 * sigmoid(vcurv, 4, -0.5), -prepare_wiggle_room, prepare_wiggle_room) if prepare_wiggle_room > 0.0 else 0.0

    # wait, if we are losing a lane, don't push us in the direction of that lane, as we are more likely to go over it
    if curve_prepare > 0 and self.rll_std > 0.5: # pushing right and losing right lane
      curve_prepare *= clamp(1.0 - 2.0 * (self.rll_std - 0.5), 0.25, 1.0)
    elif curve_prepare < -0 and self.lll_std > 0.5: # pushing left and losing left lane
      curve_prepare *= clamp(1.0 - 2.0 * (self.lll_std - 0.5), 0.25, 1.0)

    path_from_left_lane = self.lll_y + lane_distance + curve_prepare
    path_from_right_lane = self.rll_y - lane_distance + curve_prepare

    self.d_prob = l_prob + r_prob - l_prob * r_prob

    safe_idxs = np.isfinite(self.ll_t)
    if safe_idxs[0] and l_prob + r_prob > 0.9:
      lane_path_y = (l_prob * path_from_left_lane + r_prob * path_from_right_lane)
      path_xyz[:,1] = self.lane_change_multiplier * np.interp(path_t, self.ll_t[safe_idxs], lane_path_y[safe_idxs]) + (1 - self.lane_change_multiplier) * path_xyz[:,1]

    # apply camera offset after everything
    path_xyz[:, 1] += CAMERA_OFFSET

    return path_xyz
