#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2023/3/30 12:19 PM
# @Author  : wangdongming
# @Site    : 
# @File    : img2img.py
# @Software: Hifive
import os.path
import shutil
import time
import typing
import modules.scripts
import modules.shared as shared
import numpy as np
from enum import IntEnum
from loguru import logger
from PIL import Image, ImageOps, ImageFilter, ImageEnhance, ImageChops
from modules import deepbooru
from handlers.typex import ModelType
from modules.sd_models import CheckpointInfo
from worker.handler import TaskHandler
from modules.generation_parameters_copypaste import create_override_settings_dict
from modules.img2img import process_batch
from worker.task import TaskType, TaskProgress, Task, TaskStatus
from modules.processing import StableDiffusionProcessingImg2Img, process_images, Processed, create_binary_mask
from handlers.utils import init_script_args, get_selectable_script, init_default_script_args, format_override_settings, \
    load_sd_model_weights, save_processed_images, get_tmp_local_path, get_model_local_path, batch_model_local_paths
from handlers.extension.controlnet import exec_control_net_annotator
from worker.dumper import dumper
from tools.image import plt_show, encode_pil_to_base64
from modules import sd_models

AlwaysonScriptsType = typing.Dict[str, typing.Mapping[str, typing.Any]]
PixelDeviation = 2


class Img2ImgMinorTaskType(IntEnum):
    Default = 0
    Img2Img = 1
    Interrogate = 10
    RunControlnetAnnotator = 100


class ModelInfo:

    def __init__(self, **kwargs):
        self.name = kwargs['name']
        self.key = kwargs['key']
        self.type = ModelType(kwargs['type'])
        base_name, _ = os.path.splitext(self.name)
        hash, ex = os.path.splitext(self.key)
        self.name = base_name + ex
        self.hash = hash


def gen_mask(image: Image):
    shape = list(image.size)
    shape.append(4)  # rgba
    mask = np.full(shape, 255)
    return Image.fromarray(mask, mode="RGBA")


class Img2ImgTask(StableDiffusionProcessingImg2Img):

    def __init__(self, base_model_path: str,
                 user_id: str,
                 default_script_arg_img2img: typing.Sequence,  # 默认脚本参数，handler构造。
                 prompt: str,  # TAG
                 negative_prompt: str,  # 反向TAG
                 sampler_name: str = None,  # 采样器
                 init_img: str = None,  # 原图片（i2i，实际MODE=0）
                 sketch: str = None,  # 绘图图片（绘图，实际MODE=1）
                 init_img_with_mask: typing.Mapping[str, str] = None,
                 # 局部重绘图（手涂蒙版，实际MODE=2）,字典形式必须包含image,mask 2个KEY,蒙版为画笔白色其他地方为黑色
                 init_img_inpaint: str = None,  # 局部重绘图（上传蒙版，实际MODE=4）
                 init_mask_inpaint: str = None,  # 局部重绘蒙版图（上传蒙版，实际MODE=4）
                 inpaint_color_sketch: str = None,  # 局部重新绘制图片手绘（局部重新绘制手涂，实际MODE=3）
                 inpaint_color_sketch_orig: str = None,  # 局部重新绘制图片原图（局部重新绘制手涂，实际MODE=3）
                 batch_size: int = 1,  # 批次数量
                 mask_blur: int = 4,  # 蒙版模糊
                 n_iter: int = 1,  # 每个批次数量
                 steps: int = 30,  # 步数
                 cfg_scale: float = 7.0,  # 提示词相关性
                 image_cfg_scale: float = 1.5,  # 图片相关性，一般是隐藏的不会使用
                 width: int = 512,  # 图宽
                 height: int = 512,  # 图高
                 restore_faces: bool = False,  # 面部修复
                 tiling: bool = False,  # 可平铺
                 mode: int = 1,  # i2i 模式
                 seed: int = -1,  # 随机种子
                 seed_enable_extras: bool = False,  # 是否启用随机种子扩展
                 subseed: int = -1,  # 差异随机种子
                 subseed_strength: float = 0,  # 差异强度
                 seed_resize_from_h: int = 0,  # 重置尺寸种子-高度
                 seed_resize_from_w: int = 0,  # 重置尺寸种子-宽度
                 resize_mode: int = 0,  # 缩放模式，0-拉伸，1-裁剪，2-填充
                 inpainting_mask_invert: int = 0,  # 局部重绘（含手涂和上传）的蒙版模式，0-重绘蒙版内容，1-重绘非蒙版内容
                 inpaint_full_res: bool = False,  # 局部重绘（含手涂和上传）的重绘区域，默认原图；True代表仅蒙版
                 inpaint_full_res_padding: int = 32,  # 局部重绘（含手涂和上传）的仅蒙版模式的边缘预留像素
                 inpainting_fill: int = 1,  # 局部重绘（含手涂和上传）的蒙版蒙住的内容：0-填充，1-原图，2-潜变量，3-潜变量为0
                 mask_alpha: float = 0,  # 局部重绘制（含手涂和上传），蒙版透明度
                 denoising_strength: float = 0.75,  # 重绘幅度（含手涂和上传）
                 select_script_name: str = None,  # 选择下拉框脚本名
                 select_script_args: typing.Sequence = None,  # 选择下拉框脚本参数
                 select_script_nets: typing.Sequence[typing.Mapping] = None,  # 选择下拉框脚本涉及的模型信息
                 alwayson_scripts: AlwaysonScriptsType = None,  # 插件脚本，object格式： {插件名: {'args': [参数列表]}}
                 img2img_batch_input_dir: str = None,
                 img2img_batch_output_dir: str = None,
                 prompt_styles: typing.List[str] = None,  # 提示风格（模板风格也就是TAG模板）
                 img2img_batch_inpaint_mask_dir: str = None,
                 override_settings_texts=None,  # 自定义设置 TEXT,如: ['Clip skip: 2', 'ENSD: 31337', 'sd_vae': 'None']
                 scale_by=-1,  # 图形放大，大于0生效。
                 lora_models: typing.Sequence[str] = None,  # 使用LORA，用户和系统全部LORA列表
                 embeddings: typing.Sequence[str] = None,  # embeddings，用户和系统全部embedding列表
                 lycoris_models: typing.Sequence[str] = None,  # lycoris，用户和系统全部lycoris列表
                 disable_ad_face: bool = True,  # 关闭默认的ADetailer face
                 enable_refiner: bool = False,  # 是否启用XLRefiner
                 refiner_switch_at: float = 0.2,  # XL 精描切换时机
                 refiner_checkpoint: str = None,  # XL refiner模型文件
                 **kwargs):
        # fast模式下关闭默认的AD插件
        disable_ad_face = disable_ad_face or kwargs.get('is_fast', True)
        override_settings_texts = format_override_settings(override_settings_texts)
        override_settings = create_override_settings_dict(override_settings_texts)
        image = None
        mask = None
        self.is_batch = False
        mode -= 1  # 适配GOLANG
        if mode == 5:
            if not img2img_batch_input_dir \
                    or not img2img_batch_output_dir:
                raise ValueError('batch input or output directory is empty')
            self.is_batch = True
        elif mode == 4:
            init_img_inpaint = get_tmp_local_path(init_img_inpaint)
            if not init_mask_inpaint:
                raise ValueError('img_inpaint or mask_inpaint not found')
            image = Image.open(init_img_inpaint).convert('RGBA')
            if init_mask_inpaint:
                init_mask_inpaint = get_tmp_local_path(init_mask_inpaint)
                mask = Image.open(init_mask_inpaint).convert('RGBA')
            else:
                mask = gen_mask(image)
        elif mode == 3:
            inpaint_color_sketch = get_tmp_local_path(inpaint_color_sketch)
            if not inpaint_color_sketch:
                raise Exception('inpaint_color_sketch not found')
            image = Image.open(inpaint_color_sketch).convert('RGB')

            orig_path = inpaint_color_sketch_orig or inpaint_color_sketch
            if orig_path != inpaint_color_sketch:
                orig_path = get_tmp_local_path(inpaint_color_sketch_orig)
                orig = Image.open(orig_path).convert('RGB')
            else:
                orig = image
            # np.diff(np.sum(np.array(orig), axis=-1), np.sum(np.array(image), axis=-1))
            # relative_err_value = np.abs(np.sum(np.array(orig), axis=-1) - np.sum(np.array(image), axis=-1))
            pred = np.any(np.array(image) != np.array(orig), axis=-1)
            # pred = np.abs(
            #     np.sum(np.array(image, np.int), axis=-1) - np.sum(np.array(orig, np.int), axis=-1)
            # ) > PixelDeviation

            mask = Image.fromarray(pred.astype(np.uint8) * 255, "L")
            mask = ImageEnhance.Brightness(mask).enhance(1 - mask_alpha / 100)
            blur = ImageFilter.GaussianBlur(mask_blur)
            image = Image.composite(image.filter(blur), orig, mask.filter(blur))
            image = image.convert("RGB")
        elif mode == 2:
            if not init_img_with_mask:
                raise Exception('init_img_with_mask not found')
            if 'image' not in init_img_with_mask:
                raise Exception('image not found in init_img_with_mask')
            image_path = init_img_with_mask["image"]
            image_path = get_tmp_local_path(image_path)
            image = Image.open(image_path).convert('RGBA')

            if 'mask' not in init_img_with_mask or not init_img_with_mask["mask"]:
                mask = gen_mask(image)
            else:
                mask_path = init_img_with_mask["mask"]
                mask_path = get_tmp_local_path(mask_path)
                mask = Image.open(mask_path).convert('RGBA')

            # alpha_mask = ImageOps.invert(image.split()[-1]).convert('L').point(lambda x: 255 if x > 0 else 0, mode='1')
            # mask = ImageChops.lighter(alpha_mask, mask.convert('L')).convert('L')

            mask = create_binary_mask(mask)
            image = image.convert("RGB")
        elif mode == 1:
            sketch = get_tmp_local_path(sketch)
            if not sketch:
                raise Exception('sketch not found')
            image = Image.open(sketch).convert("RGB")
            mask = None
        elif mode == 0:
            init_img = get_tmp_local_path(init_img)
            if not init_img:
                raise Exception('init image not found')
            image = Image.open(init_img).convert("RGB")
            mask = None
        else:
            raise ValueError(f'mode value error, except 0~5 got {mode}')
        if image is not None:
            image = ImageOps.exif_transpose(image)

        if scale_by > 0:
            assert image, "Can't scale by because no image is selected"

            width = int(image.width * scale_by)
            height = int(image.height * scale_by)

        assert 0. <= denoising_strength <= 1., 'can only work with strength in [0.0, 1.0]'

        if not modules.scripts.scripts_img2img:
            modules.scripts.scripts_img2img.initialize_scripts(True)

        i2i_script_runner = modules.scripts.scripts_img2img
        selectable_scripts, selectable_script_idx = get_selectable_script(i2i_script_runner, select_script_name)
        script_args = init_script_args(default_script_arg_img2img, alwayson_scripts, selectable_scripts,
                                       selectable_script_idx, select_script_args, i2i_script_runner,
                                       not disable_ad_face, enable_refiner, refiner_switch_at, refiner_checkpoint,
                                       seed, seed_enable_extras, subseed, subseed_strength, seed_resize_from_h,
                                       seed_resize_from_w)

        self.sd_model = shared.sd_model
        self.outpath_samples = f"output/{user_id}/img2img/samples/"
        self.outpath_grids = f"output/{user_id}/img2img/grids/"
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.styles = prompt_styles
        self.seed = seed
        self.subseed = subseed
        self.subseed_strength = subseed_strength
        self.seed_resize_from_h = seed_resize_from_h
        self.seed_resize_from_w = seed_resize_from_w
        self.seed_enable_extras = seed_enable_extras
        self.sampler_name = sampler_name or 'Euler a'
        self.batch_size = batch_size if batch_size > 0 else 1
        self.n_iter = n_iter if n_iter > 0 else 1
        self.steps = steps
        self.cfg_scale = cfg_scale  # 7
        self.width = width
        self.height = height
        self.restore_faces = restore_faces
        self.tiling = tiling
        self.init_images = [image]
        self.mask = mask
        self.mask_blur = mask_blur
        self.inpainting_fill = inpainting_fill
        self.resize_mode = resize_mode
        self.denoising_strength = denoising_strength
        self.image_cfg_scale = image_cfg_scale  # 1.5
        self.inpaint_full_res = inpaint_full_res  # 0
        self.inpaint_full_res_padding = inpaint_full_res_padding  # 32
        self.inpainting_mask_invert = inpainting_mask_invert  # 0
        self.override_settings = override_settings
        self.do_not_save_samples = False
        self.outpath_scripts = f"output/{user_id}/img2img/scripts/"
        self.scripts = i2i_script_runner
        self.script_name = select_script_name
        self.base_model_path = base_model_path
        self.selectable_scripts = selectable_scripts
        self.img2img_batch_input_dir = img2img_batch_input_dir
        self.img2img_batch_output_dir = img2img_batch_output_dir
        self.img2img_batch_inpaint_mask_dir = img2img_batch_inpaint_mask_dir
        self.kwargs = kwargs
        self.loras = lora_models
        self.embedding = embeddings
        self.lycoris = lycoris_models
        self.select_script_nets = select_script_nets
        self.xl_refiner = enable_refiner
        self.refiner_switch_at = refiner_switch_at
        self.xl_refiner_model_path = refiner_checkpoint

        if selectable_scripts:
            self.script_args = script_args
        else:
            self.script_args = tuple(script_args)

        super(Img2ImgTask, self).__post_init__()
        # extra_generation_params 赋值必须得在post_init后，
        # 因为extra_generation_params初始化在post_init
        if mask:
            self.extra_generation_params["Mask blur"] = mask_blur

    def close(self):
        super(Img2ImgTask, self).close()
        for img in self.init_images:
            img.close()
        if hasattr(self.mask, "close"):
            self.mask.close()
        for obj in self.script_args:
            if hasattr(obj, 'close'):
                obj.close()
            if isinstance(obj, dict):
                for v in obj.values():
                    if hasattr(v, 'close'):
                        v.close()

    @classmethod
    def from_task(cls, task: Task, default_script_arg_img2img: typing.Sequence, refiner_checkpoint: str = None):
        base_model_path = task['base_model_path']
        alwayson_scripts = task['alwayson_scripts'] if 'alwayson_scripts' in task else None
        user_id = task['user_id']
        select_script = task.get('select_script')
        select_script_name, select_script_args = None, None
        prompt = task.get('prompt', '')
        negative_prompt = task.get('negative_prompt', '')

        if select_script:
            if not isinstance(select_script, dict):
                raise TypeError('select_script type err')
            select_script_name = select_script['name']
            select_script_args = select_script['args']
        else:
            select_script_name = task.get('select_script_name')
            select_script_args = task.get('select_script_args')

        kwargs = task.data.copy()
        kwargs.pop('base_model_path')
        kwargs.pop('prompt')
        kwargs.pop('negative_prompt')
        kwargs.pop('user_id')

        if 'alwayson_scripts' in kwargs:
            kwargs.pop('alwayson_scripts')
        if 'select_script' in kwargs:
            kwargs.pop('select_script')
        if 'select_script_name' in kwargs:
            kwargs.pop('select_script_name')
        if 'select_script_args' in kwargs:
            kwargs.pop('select_script_args')

        if "nsfw" in prompt.lower():
            prompt = prompt.lower().replace('nsfw', '')
        kwargs['refiner_checkpoint'] = refiner_checkpoint

        return cls(base_model_path,
                   user_id,
                   default_script_arg_img2img,
                   prompt=prompt,
                   negative_prompt=negative_prompt,
                   alwayson_scripts=alwayson_scripts,
                   select_script_name=select_script_name,
                   select_script_args=select_script_args,
                   **kwargs)

    @staticmethod
    def debug_task() -> typing.Sequence[Task]:
        models_dir = '/home/jty/stable-diffusion-webui-master/models/Stable-diffusion'
        models = [
            'v1-5-pruned-emaonly.ckpt',
            # 'chilloutmix_NiCkpt.ckpt',
            # 'guofeng2_v20.safetensors',
            # 'v1-5-pruned-emaonly.ckpt',
        ]
        model_hash_map = {
            'v1-5-pruned-emaonly.ckpt': 'cc6cb27103417325ff94f52b7a5d2dde45a7515b25c255d8e396c90014281516',
            'chilloutmix_NiCkpt.ckpt': '3a17d0deffa4592fd91c711a798031a258ab44041809ade8b4591c0225ea9401',
            'guofeng2_v20.safetensors': '3257896d4b399dc70cd0d2ef76f4965d309413fea1f11f1d3173e9069e3b3a92'
        }

        t = {
            'task_id': 'test_i2i',
            'base_model_path': f'{models_dir}/v1-5-pruned-emaonly.ckpt',
            'model_hash': '',
            'alwayson_scripts': {},
            'user_id': 'test_user',
            'select_script': None,
            'task_type': TaskType.Image2Image,
            'create_at': -1,
            'negative_prompt': '',
            "init_img": "test-imgs/QQ20230324-104509.png",
            'prompt': '<lora:Xiaorenshu_v20:0.6>',
            'lora_models': ['/data/apksamba/sd/models/Lora/Xiaorenshu_v20.safetensors']
        }
        # remoting task
        rt = {
            'task_id': 'test_i2i_remoting',
            'base_model_path': f'{models_dir}/v1-5-pruned-emaonly.ckpt',
            'model_hash': '',
            'alwayson_scripts': {},
            'user_id': 'test_user',
            'select_script': None,
            'task_type': TaskType.Image2Image,
            'create_at': -1,
            'negative_prompt': '',
            "init_img": "gbdata-qa/sd-webui/Images/Moxin_10.png",
            'prompt': '<lora:makimaChainsawMan_offset:0.6>, 1girl,',
            'lora_models': [
                '/data/apksamba/sd/models/Lora/Xiaorenshu_v20.safetensors',
                'gbdata-qa/sd-webui/Lora/makimaChainsawMan_offset.safetensors'
            ]
        }

        tasks = []
        for m in models:
            basename, _ = os.path.splitext(m)
            t['base_model_path'] = os.path.join(models_dir, m)
            t['task_id'] = f'test_i2i_{basename}_{len(tasks)}'
            t['model_hash'] = model_hash_map[m]

            rt['base_model_path'] = os.path.join(models_dir, m)
            rt['task_id'] = f'test_i2i_remoting_{basename}_{len(tasks)}'
            rt['model_hash'] = model_hash_map[m]
            tasks.append(Task(**t))
            tasks.append(Task(**rt))

        return tasks


class Img2ImgTaskHandler(TaskHandler):

    def __init__(self):
        super(Img2ImgTaskHandler, self).__init__(TaskType.Image2Image)
        self._default_script_args_load_t = 0

    def _refresh_default_script_args(self):
        if time.time() - self._default_script_args_load_t > 3600 * 4:
            self._load_default_script_args()

    def _load_default_script_args(self):
        if not modules.scripts.scripts_img2img:
            modules.scripts.scripts_img2img.initialize_scripts(is_img2img=True)
        self.default_script_args = init_default_script_args(modules.scripts.scripts_img2img)
        self._default_script_args_load_t = time.time()

    def _build_img2img_arg(self, progress: TaskProgress, refiner_checkpoint: str = None) -> Img2ImgTask:
        # 可不使用定时刷新，直接初始化。
        self._refresh_default_script_args()

        t = Img2ImgTask.from_task(progress.task, self.default_script_args, refiner_checkpoint)
        shared.state.current_latent_changed_callback = lambda: self._update_preview(progress)
        return t

    def _get_local_checkpoint(self, task: Task):
        '''
        下载大模型，或者脚本中的模型列表
        '''
        progress = TaskProgress.new_prepare(task, f"0%")
        xl_refiner_model_path = task.get('refiner_checkpoint')
        # 脚本任务
        self._get_select_script_models(progress)

        def progress_callback(*args):
            if len(args) < 2:
                return
            transferred, total = args[0], args[1]
            p = int(transferred * 100 / total)
            if xl_refiner_model_path:
                p = p * 0.5
            current_progress = int(float(progress.task_desc[:-1]))
            if p % 5 == 0 and p >= current_progress + 5:
                progress.task_desc = f"{p}%"
                self._set_task_status(progress)

        base_model_path = get_model_local_path(task.sd_model_path, ModelType.CheckPoint, progress_callback)
        if not base_model_path or not os.path.isfile(base_model_path):
            raise OSError(f'cannot found model:{task.sd_model_path}')

        def refiner_model_progress_callback(*args):
            if len(args) < 2:
                return
            transferred, total = args[0], args[1]
            p = int(50 + transferred * 100 * 0.5 / total)

            current_progress = int(float(progress.task_desc[:-1]))
            if p % 5 == 0 and p >= current_progress + 5:
                progress.task_desc = f"{p}%"
                self._set_task_status(progress)

        if xl_refiner_model_path:
            xl_refiner_model = get_model_local_path(
                xl_refiner_model_path, ModelType.CheckPoint, refiner_model_progress_callback)
            if not xl_refiner_model or not os.path.isfile(xl_refiner_model):
                raise OSError(f'cannot found model:{xl_refiner_model_path}')
            return base_model_path, xl_refiner_model
        else:
            return base_model_path

    def _get_local_embedding_dirs(self, embeddings: typing.Sequence[str]) -> typing.Set[str]:
        # embeddings = [get_model_local_path(p, ModelType.Embedding) for p in embeddings]
        embeddings = batch_model_local_paths(ModelType.Embedding, *embeddings)
        os.popen(f'touch {" ".join(embeddings)}')
        return set((os.path.dirname(p) for p in embeddings if p and os.path.isfile(p)))

    def _get_select_script_models(self, progress: TaskProgress):
        '''
        下载下拉脚本用到的模型列表
        '''
        task = progress.task
        select_script_nets = task.get('select_script_nets')
        if select_script_nets:
            total = len(select_script_nets)

            def dump_progress(i):
                p = int(i * 100 / total)

                current_progress = int(progress.task_desc[:-1])
                if p >= current_progress:
                    progress.task_desc = f"{p}%"
                    self._set_task_status(progress)

            logger.debug('>>> start download select_script_nets')
            for i, mi in enumerate(select_script_nets):
                dump_progress(i)
                model_info = ModelInfo(**mi)
                local = get_model_local_path(model_info.key, model_info.type)
                logger.debug(f'download {model_info.key} to {local} ')
                if not local:
                    raise OSError(f'cannot download file:{model_info.key}')

                dir = os.path.dirname(local)
                dst = os.path.join(dir, model_info.name)
                if not os.path.isfile(dst):
                    # 防止有重名导致问题~
                    shutil.copy(local, dst)
                logger.debug(f'{local} copy to {dst}')
                os.popen(f'touch {local} {dst}')

                # 修改路径
                task['select_script_nets'][i]['local'] = dst
            return True
        return False

    def _get_local_loras(self, loras: typing.Sequence[str]) -> typing.Sequence[str]:
        loras = batch_model_local_paths(ModelType.Lora, *loras)
        local_models = [p for p in loras if p and os.path.isfile(p)]
        os.popen(f'touch {" ".join(local_models)}')

        return local_models

    def _get_local_lycoris(self, lycoris: typing.Sequence[str]) -> typing.Sequence[str]:
        local_models = batch_model_local_paths(ModelType.LyCORIS, *lycoris)
        local_models = [p for p in local_models if p and os.path.isfile(p)]
        os.popen(f'touch {" ".join(local_models)}')

        return local_models

    def _set_little_models(self, process_args):
        select_script_nets = getattr(process_args, 'select_script_nets', [])
        # 下拉脚本生效
        if select_script_nets:
            loras, embeddings = [], []
            for net in select_script_nets:
                dst = net['local']
                model_info = ModelInfo(**net)
                print(f'select script nets:{dst}')

                if model_info.type == ModelType.CheckPoint:
                    basename = os.path.basename(model_info.key)
                    sha256, _ = os.path.splitext(basename)
                    checkpoint_info = CheckpointInfo(dst, sha256)
                    checkpoint_info.register()
                elif model_info.type == ModelType.Embedding:
                    embeddings.append(dst)
                elif model_info.type == ModelType.Lora:
                    loras.append(dst)
            loras.extend(process_args.loras or [])
            embeddings.extend(process_args.embedding or [])
            process_args.loras = loras
            process_args.embedding = embeddings

        if process_args.loras:
            # 设置LORA，具体实施在modules/exta_networks.py 中activate函数。
            sd_models.user_loras = self._get_local_loras(process_args.loras)
        else:
            sd_models.user_loras = []

        if process_args.embedding:
            embedding_dirs = self._get_local_embedding_dirs(process_args.embedding)
            sd_models.user_embedding_dirs = set(embedding_dirs)
        else:
            sd_models.user_embedding_dirs = []

        if process_args.lycoris:
            pass

    def _exec_img2img(self, task: Task) -> typing.Iterable[TaskProgress]:
        local_model_paths = self._get_local_checkpoint(task)
        base_model_path = local_model_paths if not isinstance(local_model_paths, tuple) else local_model_paths[0]
        refiner_checkpoint = None if not isinstance(local_model_paths, tuple) else local_model_paths[1]

        load_sd_model_weights(base_model_path, task.model_hash)
        progress = TaskProgress.new_ready(task, f'model loaded, run i2i...')
        yield progress
        # 参数有使用到sd_model因此在切换模型后再构造参数。
        process_args = self._build_img2img_arg(progress, refiner_checkpoint)
        self._set_little_models(process_args)
        # if process_args.loras:
        #     # 设置LORA，具体实施在modules/exta_networks.py 中activate函数。
        #     sd_models.user_loras = self._get_local_loras(process_args.loras)
        # else:
        #     sd_models.user_loras = []
        # if process_args.embedding:
        #     embedding_dirs = self._get_local_embedding_dirs(process_args.embedding)
        #     sd_models.user_embedding_dirs = set(embedding_dirs)
        # else:
        #     sd_models.user_embedding_dirs = []

        progress.status = TaskStatus.Running
        progress.task_desc = f'i2i task({task.id}) running'
        yield progress

        shared.state.begin()
        # shared.state.job_count = process_args.n_iter * process_args.batch_size
        inference_start = time.time()
        if process_args.is_batch:
            assert not shared.cmd_opts.hide_ui_dir_config, "Launched with --hide-ui-dir-config, batch img2img disabled"

            process_batch(process_args,
                          process_args.img2img_batch_input_dir,
                          process_args.img2img_batch_output_dir,
                          process_args.img2img_batch_inpaint_mask_dir,
                          process_args.script_args)

            processed = Processed(process_args, [], process_args.seed, "")
        else:
            if process_args.selectable_scripts:
                processed = process_args.scripts.run(process_args,
                                                     *process_args.script_args)  # Need to pass args as list here
            else:
                processed = process_images(process_args)
        shared.state.end()
        process_args.close()
        inference_time = time.time() - inference_start
        progress.status = TaskStatus.Uploading
        yield progress

        images = save_processed_images(processed,
                                       process_args.outpath_samples,
                                       process_args.outpath_grids,
                                       process_args.outpath_scripts,
                                       task.id,
                                       inspect=process_args.kwargs.get("need_audit", False),
                                       detect_multi_face=process_args.kwargs.get("detect_multi_face", False),
                                       forbidden_review=process_args.kwargs.get("forbidden_review", False))
        images.update({'inference_time': inference_time})
        progress = TaskProgress.new_finish(task, images)
        progress.update_seed(processed.all_seeds, processed.all_subseeds)

        yield progress

    def _set_task_status(self, p: TaskProgress):
        super()._set_task_status(p)
        dumper.dump_task_progress(p)

    def _update_running_progress(self, progress: TaskProgress, v: int):
        if v > 99:
            v = 99
        progress.task_progress = v
        self._set_task_status(progress)

    def _update_preview(self, progress: TaskProgress):
        if shared.state.sampling_step - shared.state.current_image_sampling_step < 5:
            return
        p = 0

        #     if job_count > 0:
        #         progress += job_no / job_count
        #     if sampling_steps > 0 and job_count > 0:
        #         progress += 1 / job_count * sampling_step / sampling_steps
        image_numbers = progress.task['n_iter'] * progress.task['batch_size']
        if image_numbers <= 0:
            image_numbers = 1
        if shared.state.job_count > 0:
            job_no = shared.state.job_no - 1 if shared.state.job_no > 0 else 0
            p += job_no / (image_numbers)
            # p += (shared.state.job_no) / shared.state.job_count
        if shared.state.sampling_steps > 0:
            p += 1 / (image_numbers) * shared.state.sampling_step / shared.state.sampling_steps

        current_progress = min(p * 100, 99)
        if current_progress < progress.task_progress:
            return

        time_since_start = time.time() - shared.state.time_start
        eta = (time_since_start / p)
        progress.task_progress = current_progress
        progress.eta_relative = int(eta - time_since_start)
        # print(f"-> progress: {progress.task_progress}, real:{p}\n")

        shared.state.set_current_image()
        if shared.state.current_image:
            current = encode_pil_to_base64(shared.state.current_image, 30)
            if current:
                progress.preview = current
            print("\n>>set preview!!\n")
        else:
            print('has prev')
        self._set_task_status(progress)

    def _exec_interrogate(self, task: Task):
        model = task.get('interrogate_model')
        img_key = task.get('image')
        img = None
        if model not in ["clip", "deepdanbooru"]:
            progress = TaskProgress.new_failed(task, f'model not found, task id: {task.id}, model: {model}')
            yield progress
        elif img_key:
            img = get_tmp_local_path(img_key)
        if not img:
            progress = TaskProgress.new_failed(task, f'download image failed:{img_key}')
            yield progress
        else:
            pil_img = Image.open(img)
            pil_img = pil_img.convert('RGB')
            if model == "clip":
                processed = shared.interrogator.interrogate(pil_img)
            else:
                processed = deepbooru.model.tag(pil_img)
            progress = TaskProgress.new_finish(task, {
                'interrogate': processed
            })
            yield progress

    def _exec(self, task: Task) -> typing.Iterable[TaskProgress]:
        minor_type = Img2ImgMinorTaskType(task.minor_type)
        if minor_type <= Img2ImgMinorTaskType.Img2Img:
            yield from self._exec_img2img(task)
        elif minor_type == Img2ImgMinorTaskType.RunControlnetAnnotator:
            yield from exec_control_net_annotator(task)
        elif minor_type == Img2ImgMinorTaskType.Interrogate:
            yield from self._exec_interrogate(task)
