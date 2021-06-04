import numpy as np
from quaternion import quaternion as quat
import cv2

import habitat
from habitat.utils.visualizations.maps import get_topdown_map_from_sim, colorize_topdown_map


class BaseSimulatorViewer:
    CV2_WAIT_TIME = 30

    def __init__(self, sim_cfg, win_basename="Simulator", scale=None, draw_origin=False):
        self.cfg = sim_cfg
        self.sim = habitat.sims.make_sim(sim_cfg.TYPE, config=sim_cfg)
        self.obs = self.sim.reset()

        self.map_win_name = win_basename + " - Map"
        cv2.namedWindow(self.map_win_name)
        cv2.setMouseCallback(self.map_win_name, self.on_mouse_map)
        self.obs_win_name = win_basename + " - Observations"
        cv2.namedWindow(self.obs_win_name)
        cv2.setMouseCallback(self.obs_win_name, self.on_mouse_obs)

        self.raw_map = get_topdown_map_from_sim(self.sim)
        self.map_img = colorize_topdown_map(self.raw_map)
        self.scale = scale
        if self.scale:
            self.map_img = cv2.resize(self.map_img, None, fx=self.scale, fy=self.scale)
        lower, upper = self.sim.pathfinder.get_bounds()
        self.map_size = (self.map_img.shape[1], self.map_img.shape[0])
        self.map_origin = np.array([lower[0], lower[2]])
        self.map_resolution = np.array([(upper[0] - lower[0]) / self.map_size[0],
                                        (upper[2] - lower[2]) / self.map_size[1]])
        self.map_altitude = self.sim.get_agent_state().position[1]
        if draw_origin:
            o = self.project_pos_to_map([0, 0, 0])
            ox = self.project_pos_to_map([1, 0, 0])
            oz = self.project_pos_to_map([0, 0, 1])
            cv2.line(self.map_img, tuple(o), tuple(ox), (0, 0, 255), 2)
            cv2.line(self.map_img, tuple(o), tuple(oz), (255, 0, 0), 2)
            cv2.circle(self.map_img, tuple(o), 5, (0, 255, 0), -1)

        self.drag_start = None
        self.drag_vec = None
        self.pin_obs = None
        self.pin_map = None
        self.running = True
        self.collision_enabled = False

    def on_mouse_map(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.drag_start = np.array([x, y]).astype(np.int64)
        elif event == cv2.EVENT_MOUSEMOVE and self.drag_start is not None:
            drag_vec = np.array([x, y]).astype(np.float32) - self.drag_start
            norm = np.sqrt(drag_vec.dot(drag_vec))
            if norm > 0:
                self.drag_vec = drag_vec / norm
                self.update()
        elif event == cv2.EVENT_LBUTTONUP:
            self.teleport_agent_on_map(self.drag_start, self.drag_vec)
            self.drag_start = None
            self.drag_vec = None
            self.update()

    def on_mouse_obs(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            if y >= self.cfg.RGB_SENSOR.HEIGHT:
                y -= self.cfg.RGB_SENSOR.HEIGHT
            self.pin_obs = (x, y)
            abs_pos = self.project_pixel_to_pos(self.pin_obs)
            self.pin_map = self.project_pos_to_map(abs_pos)
            self.update()

    def on_key(self, key_code):
        update = True
        if key_code == ord('x'):
            self.running = False
        elif key_code == ord('r'):
            self.obs = self.sim.reset()
        elif key_code == ord('c'):
            self.collision_enabled = not self.collision_enabled
        elif key_code == ord('w'):
            self.translate_agent(self.cfg.FORWARD_STEP_SIZE, 0)
        elif key_code == ord('s'):
            self.translate_agent(-self.cfg.FORWARD_STEP_SIZE, 0)
        elif key_code == ord('a'):
            self.translate_agent(0, -self.cfg.FORWARD_STEP_SIZE)
        elif key_code == ord('d'):
            self.translate_agent(0, self.cfg.FORWARD_STEP_SIZE)
        elif key_code == ord('q'):
            self.rotate_agent(self.cfg.TURN_ANGLE)
        elif key_code == ord('e'):
            self.rotate_agent(-self.cfg.TURN_ANGLE)
        else:
            update = False
        return update

    def teleport_agent_on_map(self, pos, head=None):
        pos = self.project_map_to_pos(self.drag_start)
        if head is None:
            rot = [0.0, 1.0, 0.0, 0.0]
        else:
            yaw = np.pi + np.arctan2(head[0], head[1])
            rot = [0, np.sin(0.5 * yaw), 0, np.cos(0.5 * yaw)]
        if self.collision_enabled:
            pos = self.sim.step_filter(pos, pos)
        self.obs = self.sim.get_observations_at(pos, rot, True)

    def translate_agent(self, forward=0, right=0):
        if forward == 0 and right == 0:
            return
        s = self.sim.get_agent_state()
        q = quat(0, right, 0, -forward)
        move = s.rotation * q * s.rotation.conjugate()
        pos = s.position + move.vec
        if self.collision_enabled:
            pos = self.sim.step_filter(s.position, pos)
        self.obs = self.sim.get_observations_at(pos, s.rotation, True)

    def rotate_agent(self, angle):
        s = self.sim.get_agent_state()
        angle = np.radians(angle)
        q = quat(np.cos(0.5 * angle), 0, np.sin(0.5 * angle), 0)
        rot = q * s.rotation
        if self.collision_enabled:
            pos = self.sim.step_filter(s.position, s.position)
        else:
            pos = s.position
        self.obs = self.sim.get_observations_at(pos, rot, True)

    def project_pixel_to_pos(self, pix):
        d = self.obs["depth"][pix[1], pix[0]]
        if self.cfg.DEPTH_SENSOR.NORMALIZE_DEPTH:
            d = (self.cfg.DEPTH_SENSOR.MAX_DEPTH - self.cfg.DEPTH_SENSOR.MIN_DEPTH) * d \
                    + self.cfg.DEPTH_SENSOR.MIN_DEPTH

        dim = np.array([self.cfg.DEPTH_SENSOR.WIDTH, self.cfg.DEPTH_SENSOR.HEIGHT])
        f = dim[0] / np.tan(np.radians(0.5 * self.cfg.DEPTH_SENSOR.HFOV))

        rel_pos = d * np.array([(pix[0] - 0.5 * dim[0]) / f, (0.5 * dim[1] - pix[1]) / f, -1])

        s = self.sim.get_agent_state().sensor_states["depth"]
        return (s.rotation * quat(0, *rel_pos) * s.rotation.conjugate()).vec + s.position

    def project_pos_to_map(self, pos):
        return ((pos[::2] - self.map_origin) / self.map_resolution).astype(np.int64)

    def project_map_to_pos(self, uv):
        xz = uv * self.map_resolution + self.map_origin
        return np.array([xz[0], self.map_altitude, xz[1]])

    def draw_agent_on_map(self, disp, pos=None, head=None, color=(255, 0, 0)):
        if pos is None:
            s = self.sim.get_agent_state()
            pos = self.project_pos_to_map(s.position)
            q = quat(0, 0, 0, -1)
            head = (s.rotation * q * s.rotation.conjugate()).vec[::2]
        elif head is None:
            head = np.array([0.0, 1.0])
        cv2.circle(disp, tuple(pos), 5, color, -1)
        end = (pos + 10 * head).astype(np.int64)
        cv2.line(disp, tuple(pos), tuple(end), color, 3)

    def draw_map(self):
        disp = self.map_img.copy()
        self.draw_agent_on_map(disp)
        if self.drag_vec is not None:
            self.draw_agent_on_map(disp, self.drag_start, self.drag_vec, (0, 255, 0))
        if self.pin_map is not None:
            cv2.circle(disp, tuple(self.pin_map), 3, (0, 0, 255))
        return disp

    def draw_obs(self):
        disp_depth = cv2.cvtColor((self.obs["depth"] * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
        disp = np.vstack((self.obs["rgb"][:, :, ::-1], disp_depth))
        if self.scale:
            disp = cv2.resize(disp, None, fx=self.scale, fy=self.scale)
        if self.pin_obs is not None:
            cv2.circle(disp, tuple(self.pin_obs), 3, (0, 0, 255))
            pin2 = (self.pin_obs[0], self.pin_obs[1] + self.cfg.RGB_SENSOR.HEIGHT)
            cv2.circle(disp, pin2, 3, (0, 0, 255))
        return disp

    def update(self):
        cv2.imshow(self.map_win_name, self.draw_map())
        cv2.imshow(self.obs_win_name, self.draw_obs())

    def time_update(self, dt):
        pass

    def run(self):
        self.running = True
        self.update()
        while self.running:
            c = cv2.waitKey(self.CV2_WAIT_TIME)
            self.time_update(self.CV2_WAIT_TIME)
            if c > 0 and self.on_key(c):
                self.update()

