import rospy

from tr_drive.util.debug import Debugger
from tr_drive.util.namespace import DictRegulator
from tr_drive.util.conversion import Frame

from tr_drive.sensor.odometry import Odom
from tr_drive.sensor.camera import Camera
from tr_drive.controller.goal_controller import GoalController

from tr_drive.persistent.recording import Recording


"""
    重复.
    
    is_ready():
        为 True 时方允许: 启动; 暂停; 继续; 处理 image 和 odom 消息.
        要求: recording 和 params 已初始化; devices 已初始化且 ready.
"""
class Repeater:
    def __init__(self):
        # private
        self.debugger: Debugger = Debugger(name = 'repeater_debugger')
        self.recording_and_params_initialized = False
        
        # public
        self.recording: Recording = None
        self.repeating_launched = False
        self.repeating_paused = False
        
        # parameters
        self.params = DictRegulator(rospy.get_param('/tr'))
        self.recording = Recording.from_path(self.params.persistent.recording_path)
        self.params.camera.add('patch_size', self.recording.params['image']['patch_size'])
        self.params.camera.add('resize', self.recording.params['image']['resize'])
        self.params.camera.add('horizontal_fov', self.recording.params['image']['horizontal_fov'])
        self.recording_and_params_initialized = True
        
        # devices
        self.init_devices()
    
    def init_devices(self):
        self.camera = None
        self.odometry = None
        self.controller = None
        
        self.camera = Camera(
            raw_image_topic = self.params.camera.raw_image_topic,
            patch_size = self.params.camera.patch_size,
            resize = self.params.camera.resize,
            horizontal_fov = self.params.camera.horizontal_fov,
            processed_image_topic = self.params.camera.processed_image_topic
        )
        self.camera.wait_until_ready()
        
        self.odometry = Odom(
            odom_topic = self.params.odometry.odom_topic,
            processed_odom_topic = self.params.odometry.processed_odom_topic
        )
        self.camera.register_image_received_hook(self.image_received)
        self.odometry.register_odom_received_hook(self.odom_received)
        self.odometry.wait_until_ready()
        
        self.controller = GoalController(
            cmd_vel_topic = self.params.controller.cmd_vel_topic,
            k_rho = self.params.controller.k_rho,
            k_alpha = self.params.controller.k_alpha,
            k_beta = self.params.controller.k_beta,
            k_theta = self.params.controller.k_theta,
            velocity_min = self.params.controller.velocity_min,
            velocity_max = self.params.controller.velocity_max,
            omega_min = self.params.controller.omega_min,
            omega_max = self.params.controller.omega_max,
            translation_tolerance = self.params.controller.translation_tolerance,
            rotation_tolerance = self.params.controller.rotation_tolerance
        )
        self.controller.set_odometry(self.odometry)
        self.controller.wait_until_ready()
    
    def is_ready(self):
        return \
            self.recording_and_params_initialized and \
            self.camera is not None and self.camera.is_ready() and \
            self.odometry is not None and self.odometry.is_ready() and \
            self.controller is not None and self.controller.is_ready()
    
    def start_repeating(self):
        if not self.is_ready() or self.repeating_launched:
            return
        self.odometry.zeroize()
        pass # TODO
        self.repeating_launched = True
    
    def resume_repeating(self):
        if not self.is_ready() or not self.repeating_launched or not self.repeating_paused:
            return
        pass # TODO
        self.repeating_paused = False
    
    def pause_repeating(self):
        if not self.is_ready() or not self.repeating_launched or self.repeating_paused:
            return
        pass # TODO
        self.repeating_paused = True
    
    def is_running(self):
        return self.repeating_launched and (not self.repeating_paused)
    
    def image_received(self, **args):
        if not self.is_ready() or not self.is_running():
            return False
        
        pass
        return True
            
        # if self.first_image is None:
        #     self.first_image = args['image']
        # current_image: DigitalImage = args['image']
        # r, c = current_image.height, current_image.width
        
        # offsets = np.arange(1 - c, c).astype(int)
        # correlations = ImageProcessor.horizontal_NCC(current_image, self.first_image, offsets)
        # values = np.clip((r - (np.array(correlations)) * r).astype(int), 0, r - 1)
        
        # diagram_image = DigitalImage(np.array(np.zeros((r, 2 * c - 1, 1)), dtype = np.uint8))
        # diagram_image.data[values, offsets + c - 1] = [255]
        # diagram_image.data[:, np.argmax(correlations)] = [255]
        
        # self.debugger.publish('diagram', diagram_image.to_Image(encoding = 'mono8'))

    def odom_received(self, **args):
        if not self.is_ready() or not self.is_running():
            return False
        
        pass
        return True


# TODO: idea: 金字塔匹配辅助确认距离; 互相关加权，倾向小角度; 角度校正跳变处理（例如跨度过大则找其他尖峰等）