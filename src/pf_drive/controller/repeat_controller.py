import time

import numpy as np

import transforms3d as t3d

from multinodes import Node

from pf_drive.util import t3d_ext, ROSContext, ListQueue
from pf_drive.util.img import NCC_horizontal_match, np_to_Image


"""
    `processed_image`, input (shared_object)
        format: cv2 image (np.array)
    `odom`, input (pipe)
        format: 4x4 np.array
    `record`, input (queue)
        format: (image, odom)
    `passed_goal`, output (queue)
        format: int
    `actuator_command`, output (pipe)
        format:
            (type = 'vw', v, w)
            (type = 'vphi', v, phi)
"""
class BaselineRepeatController(Node):
    def __init__(self, name, **kwargs):
        super().__init__(name)
        
        # 参数
        self.horizontal_fov = kwargs['horizontal_fov']
        self.along_path_radius = kwargs['along_path_radius'] # r
        self.steering_predict_goals = kwargs['steering_predict_goals'] # p, TODO: 注意弯道的相对位置并没有补偿误差, p 过大会导致转向轨迹太贴近 record 中有误差的值.
        self.steering_weights = kwargs['steering_weights']
        self.slowing_predict_goals = kwargs['slowing_predict_goals'] # s

        self.k_rotation = kwargs['k_rotation']
        self.k_along_path = kwargs['k_along_path']

        self.initial_compensation_rotation_update_rate = kwargs['initial_compensation_rotation_update_rate']
        self.initial_compensation_translation_update_rate = kwargs['initial_compensation_translation_update_rate']
        self.initial_compensation_rotation_threshold = kwargs['initial_compensation_rotation_threshold']
        self.initial_compensation_translation_threshold = kwargs['initial_compensation_translation_threshold']

        self.l = kwargs['track']
        self.d = kwargs['wheelbase']
        self.r = kwargs['wheel_radius']
        self.max_phi = kwargs['max_steering_angle']
        self.R_min_abs = self.d / np.tan(self.max_phi) + self.l / 2

        self.distance_threshold = kwargs['distance_threshold']
        self.reference_velocity = kwargs['reference_velocity']

        self.along_path_debug_image_topic = kwargs.get('along_path_debug_image_topic', None)

        # 滑动窗口队列
        self.max_rps = max(self.along_path_radius, self.steering_predict_goals, self.slowing_predict_goals)
        self.max_rp = max(self.along_path_radius, self.steering_predict_goals)
        self.q_r = self.along_path_radius
        self.q_size = self.along_path_radius + 1 + self.max_rps
        """
            0   1   2   3   4   5   6   7   8   9
            x   x   D0  D1  D2  D3  D4  D5  D6  D7
           |- r -|  ^  |- r -|
                    r  |----------- p -----------|
        """
        self.q = ListQueue(size = self.q_size) # (image, odom)

        # 运行时
        self.goal_distances = [0.0] # distances between goal 0 and i
        self.goal_idx = -self.max_rps - 1 # goal just passed

        self.T_0_odomA = None
        self.T_0_odomB = None
        
        self.record_T_odomA_odomB = None
        self.record_T_odomA_odomB_rotation = None
        self.record_T_odomA_odomB_translation = None

        self.initial_compensation_rotation_factor = 1.0
        self.initial_compensation_translation_factor = 1.0
    
    def pass_to_next_goal(self):
        # loader 读完后不断发送 None, 此处不断入队, 直到 q_passed_idx + 1 项也为 None 时即结束
        self.q.push(self.io['record'].read(block = True))

        # 更新 passed_goal 并输出到端口
        self.goal_idx += 1
        if self.goal_idx >= 0:
            self.io['passed_goal'].write(self.goal_idx)

        # rec_A, rec_B 有效则更新 T_0_odomA, T_0_odomB
        if self.q[self.q_r] is not None and self.q[self.q_r + 1] is not None:
            # 若 T_0_odomA 和 T_0_odomB 有值, 即进行了一轮迭代, 记录迭代后校正的程度
            if self.T_0_odomA is not None and self.T_0_odomB is not None:
                corrected_T_odomA_odomB = t3d_ext.einv(self.T_0_odomA) @ self.T_0_odomB
                rotation_factor = t3d.euler.mat2euler(corrected_T_odomA_odomB[:3, :3])[2] / self.record_T_odomA_odomB_rotation if abs(self.record_T_odomA_odomB_rotation) > self.initial_compensation_rotation_threshold else self.initial_compensation_rotation_factor
                translation_factor = t3d_ext.norm(corrected_T_odomA_odomB[:3, 3]) / self.record_T_odomA_odomB_translation if self.record_T_odomA_odomB_translation > self.initial_compensation_translation_threshold else self.initial_compensation_translation_factor
                
                self.initial_compensation_rotation_factor = self.initial_compensation_rotation_update_rate * rotation_factor + (1 - self.initial_compensation_rotation_update_rate) * self.initial_compensation_rotation_factor
                self.initial_compensation_translation_factor = self.initial_compensation_translation_update_rate * translation_factor + (1 - self.initial_compensation_translation_update_rate) * self.initial_compensation_translation_factor

            # 若 T_0_odomB 无值则赋当前 odom 值; 更新 A 为 B
            if self.T_0_odomB is None:
                self.T_0_odomB = self.io['odom'].read(block = True)
            self.T_0_odomA = self.T_0_odomB

            # record 中的 T_odomA_odomB
            self.record_T_odomA_odomB = t3d_ext.einv(self.q[self.q_r][1]) @ self.q[self.q_r + 1][1] # T_odomA_odomB = T_{rec_r}_{rec_(r+1)} = inv(T_0_{rec_r}) * T_0_{rec_(r+1)}
            self.record_T_odomA_odomB_rotation = t3d.euler.mat2euler(self.record_T_odomA_odomB[:3, :3])[2]
            self.record_T_odomA_odomB_translation = t3d_ext.norm(self.record_T_odomA_odomB[:3, 3])

            # 根据先前的误差程度粗调 T_odomA_odomB 作为初始估计
            T_odomA_odomB = self.record_T_odomA_odomB.copy()
            if self.record_T_odomA_odomB_rotation > self.initial_compensation_rotation_threshold:
                T_odomA_odomB[:3, :3] = t3d.euler.euler2mat(0, 0, t3d.euler.mat2euler(T_odomA_odomB[:3, :3])[2] * self.initial_compensation_rotation_factor)
            if self.record_T_odomA_odomB_translation > self.initial_compensation_translation_threshold:
                T_odomA_odomB[:3, 3] *= self.initial_compensation_translation_factor
            self.T_0_odomB = self.T_0_odomA @ T_odomA_odomB

        # 最新入队两个元素有效则更新 goal_distances
        if self.q[-2] is not None and self.q[-1] is not None:
            t_delta = self.q[-1][1] - self.q[-2][1]
            self.goal_distances.append(self.goal_distances[-1] + t3d_ext.norm(t3d_ext.edt(t_delta)))
    
    def run(self):
        ros = ROSContext(self.name)
        ros.init_node(anonymous = False)

        # 检查接口
        while not ros.is_shutdown():
            if 'processed_image' not in self.io or 'odom' not in self.io or 'actuator_command' not in self.io or 'passed_goal' not in self.io or 'record' not in self.io:
                time.sleep(0.1)
                continue
            break

        # 常用量
        r = self.along_path_radius
        rps = self.max_rps
        rp = self.max_rp

        v_full = self.reference_velocity
        v_low = 2.0 # TODO
        weights = np.pad(self.steering_weights, (0, rps - len(self.steering_weights)), 'constant', constant_values = 0)

        v_target = v_full

        # 凑满 q_size 个数据, 初始皆为 None, 从 passed_idx 为 -self.max_rp - 1 开始逐个入队直到 0
        self.q.q = [None] * self.q_size
        while not self.goal_idx == 0:
            self.pass_to_next_goal()

        # 主循环
        timer_fps = timer_P = time.time()
        operation_num = 0
        while not ros.is_shutdown():
            current_time = time.time()
            if current_time - timer_fps > 2.0:
                print('fps:', operation_num / (time.time() - timer_fps))
                operation_num = 0
                timer_fps = current_time

            # 结束
            if self.q[r] is not None and self.q[r + 1] is None:
                print('Finished.')
                self.io['passed_goal'].write(None) # 结束信号
                # self.io['actuator_command'].write(('vw', 0, 0)) # 由 __main__ 进行停车
                break
            
            # 运算, TODO: 提高 correction 的影响力, 降低初始估计的贡献 (例如录制时在某处里程计较不准, repeat 时过去估计的惯性会导致无法即时响应); or 录制使用视觉里程计
            if self.io['processed_image'].poll() and self.io['odom'].poll():
                image = self.io['processed_image'].read()
                odom = self.io['odom'].read() # odom, R: robot

                i = self.goal_idx # q_idx - r + i = goal_idx

                T_0_odomR = odom
                T_odomR_odomB = t3d_ext.einv(T_0_odomR) @ self.T_0_odomB

                t_0_odomA = t3d_ext.edt(self.T_0_odomA)
                t_0_odomB = t3d_ext.edt(self.T_0_odomB)
                t_0_odomR = t3d_ext.edt(T_0_odomR)

                t_odomA_odomB = t_0_odomB - t_0_odomA
                t_odomA_odomR = t_0_odomR - t_0_odomA
                t_odomR_odomB = t3d_ext.edt(T_odomR_odomB)

                l_odomA_odomB = t3d_ext.norm(t_odomA_odomB)
                l_odomR_odomB = t3d_ext.norm(t_odomR_odomB)

                turning_goal = l_odomA_odomB < self.distance_threshold
                if not turning_goal:
                    l_proj_odomA_odomR = np.dot(t_odomA_odomR, t_odomA_odomB) / l_odomA_odomB
                    u = l_proj_odomA_odomR / l_odomA_odomB # not turning_goal

                    dt = current_time - timer_P
                    timer_P = current_time

                    # along-path correction
                    scan_q_indices = [q_idx for q_idx in range(2 * r + 1) if self.q[q_idx] is not None]
                    scan_q_indices = [scan_q_indices[0]] * (scan_q_indices[0]) + scan_q_indices
                    scan_q_indices = scan_q_indices + [scan_q_indices[-1]] * (2 * r - scan_q_indices[-1])
                    scan_distances = np.array([self.goal_distances[q_idx - r + i] for q_idx in scan_q_indices]) - self.goal_distances[i] - l_proj_odomA_odomR
                    scan_offsets, scan_values = np.zeros((2, len(scan_q_indices)))

                    if self.along_path_debug_image_topic is not None: # [debug]
                        debug_img = None
                        dash_img = np.zeros_like(image)[:5, :]
                    for k, q_idx in enumerate(scan_q_indices):
                        if q_idx == r:
                            k_r = k
                        img_ref = self.q[q_idx][0]
                        scan_offsets[k], scan_values[k] = NCC_horizontal_match(image, img_ref)
                        if self.along_path_debug_image_topic is not None: # [debug]
                            if debug_img is None:
                                debug_img = img_ref.copy()
                            else:
                                debug_img = np.concatenate((debug_img, dash_img, img_ref), axis = 0)
                                if q_idx == r:
                                    debug_img = np.concatenate((debug_img, dash_img, image), axis = 0)
                    if self.along_path_debug_image_topic is not None: # [debug]
                        ros.publish_topic(self.along_path_debug_image_topic, np_to_Image(debug_img))

                    # [Approach 1] 保留最大值附近的值
                    # scan_values[abs(np.arange(len(scan_values)) - np.argmax(scan_values)) > 1] = 0
                    # [Approach 2] 低于次高值的全部置零
                    scan_values[scan_values < scan_values[scan_values != scan_values.max()].max()] = 0
                    # [Approach 3] 随意的设置, 理应表示 NCC 底噪; 如果全都被滤除说明两图差距已经很大, 也许可以作为确认丢失的一种条件; 最小值 * 0.8 仅为防止崩溃, 无实际意义.
                    # scan_values[scan_values < min(0.1, scan_values.min() * 0.8)] = 0
                    # [Approach 4]
                    # scan_values -= np.mean(scan_values)
                    # scan_values[scan_values < 0] = 0
                    # [Approach 5]
                    # scan_values[scan_values < scan_values.max()] = 0

                    delta_p_distance = scan_values / scan_values.sum() @ scan_distances
                    # along_path_correction = (l_odomR_odomB - self.k_along_path * dt * delta_p_distance) / l_odomR_odomB # 0.75
                    along_path_correction = (l_odomR_odomB - self.k_along_path * delta_p_distance) / l_odomR_odomB # 0.01

                    if u > 1.0 - 1e-2 or l_odomR_odomB < self.distance_threshold:
                        self.pass_to_next_goal()
                        continue

                    # rotation correction
                    theta_A = scan_offsets[k_r] / image.shape[1] * self.horizontal_fov
                    theta_B = scan_offsets[k_r + 1] / image.shape[1] * self.horizontal_fov
                    theta_R = (1 - u) * theta_A + u * theta_B
                    rotation_correction = -self.k_rotation * dt * theta_R

                    # 优化 T_0_odomB
                    correction_offset = t3d_ext.etR([0, 0, 0], t3d.euler.euler2mat(0, 0, rotation_correction)) @ T_odomR_odomB
                    correction_offset[:3, 3] *= along_path_correction
                    self.T_0_odomB = T_0_odomR @ correction_offset
                
                # delta distance 判断 (似乎不需要)
                # corrected_T_odomR_odomB = t3d_ext.einv(T_0_odomR) @ self.T_0_odomB # 经过校正, 与 pass_to_next_goal 中的 T_odomA_odomB 不同
                
                # 发布调试话题
                ros.publish_topic('/a', t3d_ext.e2PS(self.T_0_odomA, frame_id = 'odom'))
                ros.publish_topic('/b', t3d_ext.e2PS(self.T_0_odomB, frame_id = 'odom'))
                ros.publish_topic('/r', t3d_ext.e2PS(T_0_odomR, frame_id = 'odom'))
                
                # 执行器 [Approach 1: r + 2 预测]
                # v = self.reference_velocity

                # T_0_qN = [item for item in self.q.q[r:(r + 3)] if item is not None][-1][1]
                # T_0_qB = self.q[r + 1][1]
                # T_0_odomN = self.T_0_odomB @ t3d_ext.einv(T_0_qB) @ T_0_qN
                # T_odomR_odomN = t3d_ext.einv(T_0_odomR) @ T_0_odomN

                # dy = T_odomR_odomN[1, 3]
                # dx = T_odomR_odomN[0, 3]
                # ros.publish_topic('/goal', t3d_ext.e2PS(T_0_odomN, frame_id = 'odom'))

                # if abs(dy) < 1e-2:
                #     w = 0.0
                # else:
                #     d_square = dx ** 2 + dy ** 2
                #     R = d_square / 2 / dy
                #     if abs(R) < self.R_min_abs:
                #         R = np.sign(R) * self.R_min_abs
                #     w = v / R

                # self.io['actuator_command'].write(('vw', v, w))
                # operation_num += 1

                # 执行器 [Approach 2: 加权预测], TODO: velocity control
                p_indices = np.array([q_idx for q_idx in range(r + 1, r + rp + 1) if self.q[q_idx] is not None])
                T_0_Qi = np.array([self.q[q_idx][1] for q_idx in p_indices])
                T_odomR_odomQi = (t3d_ext.einv(T_0_odomR) @ self.T_0_odomB @ t3d_ext.einv(T_0_Qi[0])) @ T_0_Qi
                
                xy = np.array([item[:2, 3] for item in T_odomR_odomQi])

                d_square = np.sum(xy * xy, axis = 1)
                with np.errstate(divide = 'ignore', invalid = 'ignore'):
                    R = d_square / 2 / xy[:, 1]
                    flag = abs(R) < self.R_min_abs
                    R[flag] = np.sign(R[flag]) * self.R_min_abs

                    w = v_full / R
                    w[np.isnan(w)] = 0.0

                    weights_q = weights[p_indices - (r + 1)]
                    w_target = weights_q @ w / np.sum(weights_q)

                    if np.isnan(w_target):
                        w_target = 0.0
                
                # 速度控制, TODO: 预测量的参数似乎应该使用距离，此处再利用距离取相应的 odom 个数，否则就与 record 耦合了
                s_indices = np.array([q_idx for q_idx in range(r + rps + 1) if self.q[q_idx] is not None])

                # [Approach 1]
                # yaws = np.array([t3d.euler.mat2euler(self.q[q_idx][1][:3, :3])[2] for q_idx in s_indices])
                # yaw_r = t3d.euler.mat2euler(self.q[r][1][:3, :3])[2]
                # yaw_diffs = np.abs(yaws - yaw_r)
                # flag = yaw_diffs > np.pi
                # yaw_diffs[flag] = 2 * np.pi - yaw_diffs[flag]

                # v_target TODO

                # [Approach 2] 队内点相对当前点所需的转弯半径取最小值; 按理说应该是局部曲率而不是全都相对当前点.
                xy_0_Qi = np.array([self.q[q_idx][1][:2, 3] for q_idx in s_indices])
                vs = xy_0_Qi[1:] - xy_0_Qi[:-1]
                ls = np.linalg.norm(vs, axis = 1)
                with np.errstate(divide = 'ignore', invalid = 'ignore'):
                    phis_abs = np.abs(np.arccos(np.clip(np.sum(vs[:-1] * vs[1:], axis = 1) / (ls[:-1] * ls[1:]), -1.0, 1.0)))
                    Rs_abs = self.d / np.tan(phis_abs)
                    R_min = Rs_abs.min()
                
                if np.isnan(R_min):
                    v_target = v_full
                else:
                    offset = max(0, R_min - self.R_min_abs) # TODO
                    k = 0.75 # TODO
                    v_target = (1 - np.exp(-offset)) * k * (v_full - v_low) + v_low # TODO
                    w_target = v_target * w_target / v_full
                
                if np.isnan(w_target):
                    w_target = 0.0
                
                # print(R_min, v_target, w_target)
                # print('\n')

                self.io['actuator_command'].write(('vw', v_target, w_target))
                operation_num += 1

                # 队内原始 odom 路径调试话题, Qr 与 T_0_odomA 对齐
                aligned_q_indices = np.array([q_idx for q_idx in range(r, r + rps + 1) if self.q[q_idx] is not None])
                aligned_q_odoms = np.array([self.q[q_idx][1] for q_idx in aligned_q_indices])
                aligned_q_odoms = self.T_0_odomA @ t3d_ext.einv(aligned_q_odoms[0]) @ aligned_q_odoms
                ros.publish_topic('/recorded_odoms', t3d_ext.es2P(aligned_q_odoms, frame_id = 'odom'))

