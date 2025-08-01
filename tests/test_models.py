"""Run tests for all models

Tests that run on CI should have a specific marker, e.g. @pytest.mark.base. This
marker is used to parallelize the CI runs, with one runner for each marker.

If new tests are added, ensure that they use one of the existing markers
(documented in pyproject.toml > pytest > markers) or that a new marker is added
for this set of tests. If using a new marker, adjust the test matrix in
.github/workflows/tests.yml to run tests with this new marker, otherwise the
tests will be skipped on CI.

"""

import pytest
import torch
import platform
import os
import fnmatch

_IS_MAC = platform.system() == 'Darwin'

try:
    from torchvision.models.feature_extraction import create_feature_extractor, get_graph_node_names, NodePathTracer
    has_fx_feature_extraction = True
except ImportError:
    has_fx_feature_extraction = False

import timm
from timm import list_models, list_pretrained, create_model, set_scriptable, get_pretrained_cfg_value
from timm.layers import Format, get_spatial_dim, get_channel_dim
from timm.models import get_notrace_modules, get_notrace_functions

import importlib
import os

torch_backend = os.environ.get('TORCH_BACKEND')
if torch_backend is not None:
    importlib.import_module(torch_backend)
torch_device = os.environ.get('TORCH_DEVICE', 'cpu')
timeout = os.environ.get('TIMEOUT')
timeout120 = int(timeout) if timeout else 120
timeout240 = int(timeout) if timeout else 240
timeout360 = int(timeout) if timeout else 360

if hasattr(torch._C, '_jit_set_profiling_executor'):
    # legacy executor is too slow to compile large models for unit tests
    # no need for the fusion performance here
    torch._C._jit_set_profiling_executor(True)
    torch._C._jit_set_profiling_mode(False)

# models with forward_intermediates() and support for FeatureGetterNet features_only wrapper
FEAT_INTER_FILTERS = [
    'vision_transformer', 'vision_transformer_sam', 'vision_transformer_hybrid', 'vision_transformer_relpos',
    'beit', 'mvitv2', 'eva', 'cait', 'xcit', 'volo', 'twins', 'deit', 'swin_transformer', 'swin_transformer_v2',
    'swin_transformer_v2_cr', 'maxxvit', 'efficientnet', 'mobilenetv3', 'levit', 'efficientformer', 'resnet',
    'regnet', 'byobnet', 'byoanet', 'mlp_mixer', 'hiera', 'fastvit', 'hieradet_sam2', 'aimv2*', 'tnt',
    'tiny_vit', 'vovnet', 'tresnet', 'rexnet', 'resnetv2', 'repghost', 'repvit', 'pvt_v2', 'nextvit', 'nest',
    'mambaout', 'inception_next', 'inception_v4', 'hgnet', 'gcvit', 'focalnet', 'efficientformer_v2', 'edgenext',
    'davit', 'rdnet', 'convnext', 'pit', 'starnet', 'shvit', 'fasternet', 'swiftformer', 'ghostnet', 'naflexvit'
]

# transformer / hybrid models don't support full set of spatial / feature APIs and/or have spatial output.
NON_STD_FILTERS = [
    'vit_*', 'naflexvit*', 'tnt_*', 'pit_*', 'coat_*', 'cait_*', '*mixer_*', 'gmlp_*', 'resmlp_*', 'twins_*',
    'convit_*', 'levit*', 'visformer*', 'deit*', 'xcit_*', 'crossvit_*', 'beit*', 'aimv2*', 'swiftformer_*',
    'poolformer_*', 'volo_*', 'sequencer2d_*', 'mvitv2*', 'gcvit*', 'efficientformer*', 'sam_hiera*',
    'eva_*', 'flexivit*', 'eva02*', 'samvit_*', 'efficientvit_m*', 'tiny_vit_*', 'hiera_*', 'vitamin*', 'test_vit*',
]
NUM_NON_STD = len(NON_STD_FILTERS)

# exclude models that cause specific test failures
if 'GITHUB_ACTIONS' in os.environ:
    # GitHub Linux runner is slower and hits memory limits sooner than MacOS, exclude bigger models
    EXCLUDE_FILTERS = [
        '*efficientnet_l2*', '*resnext101_32x48d', '*in21k', '*152x4_bitm', '*101x3_bitm', '*50x3_bitm',
        '*nfnet_f3*', '*nfnet_f4*', '*nfnet_f5*', '*nfnet_f6*', '*nfnet_f7*', '*efficientnetv2_xl*',
        '*resnetrs350*', '*resnetrs420*', 'xcit_large_24_p8*', '*huge*', '*giant*', '*gigantic*',
        '*enormous*', 'maxvit_xlarge*', 'regnet*1280', 'regnet*2560', '*_1b_*', '*_3b_*']
    NON_STD_EXCLUDE_FILTERS = ['*huge*', '*giant*',  '*gigantic*', '*enormous*', '*_1b_*', '*_3b_*']
else:
    EXCLUDE_FILTERS = ['*enormous*']
    NON_STD_EXCLUDE_FILTERS = ['*gigantic*', '*enormous*', '*_3b_*']

EXCLUDE_JIT_FILTERS = ['hiera_*', '*naflex*']

TARGET_FWD_SIZE = MAX_FWD_SIZE = 384
TARGET_BWD_SIZE = 128
MAX_BWD_SIZE = 320
MAX_FWD_OUT_SIZE = 448
TARGET_JIT_SIZE = 128
MAX_JIT_SIZE = 320
TARGET_FFEAT_SIZE = 96
MAX_FFEAT_SIZE = 256
TARGET_FWD_FX_SIZE = 128
MAX_FWD_FX_SIZE = 256
TARGET_BWD_FX_SIZE = 128
MAX_BWD_FX_SIZE = 224


def _get_input_size(model=None, model_name='', target=None):
    if model is None:
        assert model_name, "One of model or model_name must be provided"
        input_size = get_pretrained_cfg_value(model_name, 'input_size')
        fixed_input_size = get_pretrained_cfg_value(model_name, 'fixed_input_size')
        min_input_size = get_pretrained_cfg_value(model_name, 'min_input_size')
    else:
        default_cfg = model.default_cfg
        input_size = default_cfg['input_size']
        fixed_input_size = default_cfg.get('fixed_input_size', None)
        min_input_size = default_cfg.get('min_input_size', None)
    assert input_size is not None

    if fixed_input_size:
        return input_size

    if min_input_size:
        if target and max(input_size) > target:
            input_size = min_input_size
    else:
        if target and max(input_size) > target:
            input_size = tuple([min(x, target) for x in input_size])
    return input_size


@pytest.mark.base
@pytest.mark.timeout(timeout240)
@pytest.mark.parametrize('model_name', list_pretrained('test_*'))
@pytest.mark.parametrize('batch_size', [1])
def test_model_inference(model_name, batch_size):
    """Run a single forward pass with each model"""
    from PIL import Image
    from huggingface_hub import snapshot_download
    import tempfile
    import safetensors

    model = create_model(model_name, pretrained=True)
    model.eval()
    pp = timm.data.create_transform(**timm.data.resolve_data_config(model=model))

    with tempfile.TemporaryDirectory()  as temp_dir:
        snapshot_download(
            repo_id='timm/' + model_name, repo_type='model', local_dir=temp_dir, allow_patterns='test/*'
        )
        rand_tensors = safetensors.torch.load_file(os.path.join(temp_dir, 'test', 'rand_tensors.safetensors'))
        owl_tensors = safetensors.torch.load_file(os.path.join(temp_dir, 'test', 'owl_tensors.safetensors'))
        test_owl = Image.open(os.path.join(temp_dir, 'test', 'test_owl.jpg'))

    with torch.inference_mode():
        rand_output = model(rand_tensors['input'])
        rand_features = model.forward_features(rand_tensors['input'])
        rand_pre_logits = model.forward_head(rand_features, pre_logits=True)
        assert torch.allclose(rand_output, rand_tensors['output'], rtol=1e-3, atol=1e-4), 'rand output does not match'
        assert torch.allclose(rand_features, rand_tensors['features'], rtol=1e-3, atol=1e-4), 'rand features do not match'
        assert torch.allclose(rand_pre_logits, rand_tensors['pre_logits'], rtol=1e-3, atol=1e-4), 'rand pre_logits do not match'

        def _test_owl(owl_input, tol=(1e-3, 1e-4)):
            owl_output = model(owl_input)
            owl_features = model.forward_features(owl_input)
            owl_pre_logits = model.forward_head(owl_features.clone(), pre_logits=True)
            assert owl_output.softmax(1).argmax(1) == 24  # owl
            assert torch.allclose(owl_output, owl_tensors['output'], rtol=tol[0], atol=tol[1]), 'owl output does not match'
            assert torch.allclose(owl_features, owl_tensors['features'], rtol=tol[0], atol=tol[1]), 'owl output does not match'
            assert torch.allclose(owl_pre_logits, owl_tensors['pre_logits'], rtol=tol[0], atol=tol[1]), 'owl output does not match'

        _test_owl(owl_tensors['input'])  # test with original pp owl tensor
        _test_owl(pp(test_owl).unsqueeze(0), tol=(1e-1, 1e-1))  # re-process from original jpg, Pillow output can change a lot btw ver


@pytest.mark.base
@pytest.mark.timeout(timeout120)
@pytest.mark.parametrize('model_name', list_models(exclude_filters=EXCLUDE_FILTERS))
@pytest.mark.parametrize('batch_size', [1])
def test_model_forward(model_name, batch_size):
    """Run a single forward pass with each model"""
    model = create_model(model_name, pretrained=False)
    model.eval()

    input_size = _get_input_size(model=model, target=TARGET_FWD_SIZE)
    if max(input_size) > MAX_FWD_SIZE:
        pytest.skip("Fixed input size model > limit.")
    inputs = torch.randn((batch_size, *input_size))
    inputs = inputs.to(torch_device)
    model.to(torch_device)
    outputs = model(inputs)

    assert outputs.shape[0] == batch_size
    assert not torch.isnan(outputs).any(), 'Output included NaNs'

    # Test that grad-checkpointing, if supported, doesn't cause model failures or change in output
    try:
        model.set_grad_checkpointing()
    except:
        # throws if not supported, that's fine
        pass
    else:
        outputs2 = model(inputs)
        if isinstance(outputs, tuple):
            outputs2 = torch.cat(outputs2)
        assert torch.allclose(outputs, outputs2, rtol=1e-4, atol=1e-5), 'Output does not match'


@pytest.mark.base
@pytest.mark.timeout(timeout120)
@pytest.mark.parametrize('model_name', list_models(exclude_filters=EXCLUDE_FILTERS, name_matches_cfg=True))
@pytest.mark.parametrize('batch_size', [2])
def test_model_backward(model_name, batch_size):
    """Run a single forward pass with each model"""
    input_size = _get_input_size(model_name=model_name, target=TARGET_BWD_SIZE)
    if max(input_size) > MAX_BWD_SIZE:
        pytest.skip("Fixed input size model > limit.")

    model = create_model(model_name, pretrained=False, num_classes=42)
    encoder_only = model.num_classes == 0  # FIXME better approach?
    num_params = sum([x.numel() for x in model.parameters()])
    model.train()

    inputs = torch.randn((batch_size, *input_size))
    inputs = inputs.to(torch_device)
    model.to(torch_device)
    outputs = model(inputs)
    if isinstance(outputs, tuple):
        outputs = torch.cat(outputs)
    outputs.mean().backward()
    for n, x in model.named_parameters():
        assert x.grad is not None, f'No gradient for {n}'
    num_grad = sum([x.grad.numel() for x in model.parameters() if x.grad is not None])

    if encoder_only:
        output_fmt = getattr(model, 'output_fmt', 'NCHW')
        feat_axis = get_channel_dim(output_fmt)
        assert outputs.shape[feat_axis] == model.num_features, f'unpooled feature dim {outputs.shape[feat_axis]} != model.num_features {model.num_features}'
    else:
        assert outputs.shape[-1] == 42
    assert num_params == num_grad, 'Some parameters are missing gradients'
    assert not torch.isnan(outputs).any(), 'Output included NaNs'


# models with extra conv/linear layers after pooling
EARLY_POOL_MODELS = (
    timm.models.EfficientVit,
    timm.models.EfficientVitLarge,
    timm.models.FasterNet,
    timm.models.HighPerfGpuNet,
    timm.models.GhostNet,
    timm.models.MetaNeXt, # InceptionNeXt
    timm.models.MobileNetV3,
    timm.models.RepGhostNet,
    timm.models.VGG,
)

@pytest.mark.cfg
@pytest.mark.timeout(timeout360)
@pytest.mark.parametrize('model_name', list_models(
    exclude_filters=EXCLUDE_FILTERS + NON_STD_FILTERS, include_tags=True))
@pytest.mark.parametrize('batch_size', [1])
def test_model_default_cfgs(model_name, batch_size):
    """Run a single forward pass with each model"""
    model = create_model(model_name, pretrained=False)
    model.eval()
    model.to(torch_device)
    assert getattr(model, 'num_classes') >= 0
    assert getattr(model, 'num_features') > 0
    assert getattr(model, 'head_hidden_size') > 0
    state_dict = model.state_dict()
    cfg = model.default_cfg

    pool_size = cfg['pool_size']
    input_size = model.default_cfg['input_size']
    output_fmt = getattr(model, 'output_fmt', 'NCHW')
    spatial_axis = get_spatial_dim(output_fmt)
    assert len(spatial_axis) == 2  # TODO add 1D sequence support
    feat_axis = get_channel_dim(output_fmt)

    if all([x <= MAX_FWD_OUT_SIZE for x in input_size]) and \
            not any([fnmatch.fnmatch(model_name, x) for x in EXCLUDE_FILTERS]):
        # output sizes only checked if default res <= 448 * 448 to keep resource down
        input_size = tuple([min(x, MAX_FWD_OUT_SIZE) for x in input_size])
        input_tensor = torch.randn((batch_size, *input_size), device=torch_device)

        # test forward_features (always unpooled) & forward_head w/ pre_logits
        outputs = model.forward_features(input_tensor)
        outputs_pre = model.forward_head(outputs, pre_logits=True)
        assert outputs.shape[spatial_axis[0]] == pool_size[0], f'unpooled feature shape {outputs.shape} != config'
        assert outputs.shape[spatial_axis[1]] == pool_size[1], f'unpooled feature shape {outputs.shape} != config'
        assert outputs.shape[feat_axis] == model.num_features, f'unpooled feature dim {outputs.shape[feat_axis]} != model.num_features {model.num_features}'
        assert outputs_pre.shape[1] == model.head_hidden_size, f'pre_logits feature dim {outputs_pre.shape[1]} != model.head_hidden_size {model.head_hidden_size}'

        # test forward after deleting the classifier, output should be poooled, size(-1) == model.num_features
        model.reset_classifier(0)
        assert model.num_classes == 0, f'Expected num_classes to be 0 after reset_classifier(0), but got {model.num_classes}'
        model.to(torch_device)
        outputs = model.forward(input_tensor)
        assert len(outputs.shape) == 2
        assert outputs.shape[1] == model.head_hidden_size, f'feature dim w/ removed classifier {outputs.shape[1]} != model.head_hidden_size {model.head_hidden_size}'
        assert outputs.shape == outputs_pre.shape, f'output shape of pre_logits {outputs_pre.shape} does not match reset_head(0) {outputs.shape}'

        # test model forward after removing pooling and classifier
        if not isinstance(model, EARLY_POOL_MODELS):
            model.reset_classifier(0, '')  # reset classifier and disable global pooling
            model.to(torch_device)
            outputs = model.forward(input_tensor)
            assert len(outputs.shape) == 4
            assert outputs.shape[spatial_axis[0]] == pool_size[0] and outputs.shape[spatial_axis[1]] == pool_size[1]

        # test classifier + global pool deletion via __init__
        if 'pruned' not in model_name and not isinstance(model, EARLY_POOL_MODELS):
            model = create_model(model_name, pretrained=False, num_classes=0, global_pool='').eval()
            model.to(torch_device)
            outputs = model.forward(input_tensor)
            assert len(outputs.shape) == 4
            assert outputs.shape[spatial_axis[0]] == pool_size[0] and outputs.shape[spatial_axis[1]] == pool_size[1]

    # check classifier name matches default_cfg
    if cfg.get('num_classes', None):
        classifier = cfg['classifier']
        if not isinstance(classifier, (tuple, list)):
            classifier = classifier,
        for c in classifier:
            assert c + ".weight" in state_dict.keys(), f'{c} not in model params'

    # check first conv(s) names match default_cfg
    first_conv = cfg['first_conv']
    if isinstance(first_conv, str):
        first_conv = (first_conv,)
    assert isinstance(first_conv, (tuple, list))
    for fc in first_conv:
        assert fc + ".weight" in state_dict.keys(), f'{fc} not in model params'


@pytest.mark.cfg
@pytest.mark.timeout(timeout360)
@pytest.mark.parametrize('model_name', list_models(filter=NON_STD_FILTERS, exclude_filters=NON_STD_EXCLUDE_FILTERS, include_tags=True))
@pytest.mark.parametrize('batch_size', [1])
def test_model_default_cfgs_non_std(model_name, batch_size):
    """Run a single forward pass with each model"""
    model = create_model(model_name, pretrained=False)
    model.eval()
    model.to(torch_device)
    assert getattr(model, 'num_classes') >= 0
    assert getattr(model, 'num_features') > 0
    assert getattr(model, 'head_hidden_size') > 0
    state_dict = model.state_dict()
    cfg = model.default_cfg

    input_size = _get_input_size(model=model)
    if max(input_size) > 320:  # FIXME const
        pytest.skip("Fixed input size model > limit.")

    input_tensor = torch.randn((batch_size, *input_size), device=torch_device)
    feat_dim = getattr(model, 'feature_dim', None)

    outputs = model.forward_features(input_tensor)
    outputs_pre = model.forward_head(outputs, pre_logits=True)
    if isinstance(outputs, (tuple, list)):
        # cannot currently verify multi-tensor output.
        pass
    else:
        if feat_dim is None:
            feat_dim = -1 if outputs.ndim == 3 else 1
        assert outputs.shape[feat_dim] == model.num_features
        assert outputs_pre.shape[1] == model.head_hidden_size

    # test forward after deleting the classifier, output should be poooled, size(-1) == model.num_features
    model.reset_classifier(0)
    assert model.num_classes == 0, f'Expected num_classes to be 0 after reset_classifier(0), but got {model.num_classes}'
    model.to(torch_device)
    outputs = model.forward(input_tensor)
    if isinstance(outputs,  (tuple, list)):
        outputs = outputs[0]
    if feat_dim is None:
        feat_dim = -1 if outputs.ndim == 3 else 1
    assert outputs.shape[feat_dim] == model.head_hidden_size, 'pooled num_features != config'
    assert outputs.shape == outputs_pre.shape

    model = create_model(model_name, pretrained=False, num_classes=0).eval()
    model.to(torch_device)
    outputs = model.forward(input_tensor)
    if isinstance(outputs, (tuple, list)):
        outputs = outputs[0]
    if feat_dim is None:
        feat_dim = -1 if outputs.ndim == 3 else 1
    assert outputs.shape[feat_dim] == model.num_features

    # check classifier name matches default_cfg
    if cfg.get('num_classes', None):
        classifier = cfg['classifier']
        if not isinstance(classifier, (tuple, list)):
            classifier = classifier,
        for c in classifier:
            assert c + ".weight" in state_dict.keys(), f'{c} not in model params'

    # check first conv(s) names match default_cfg
    first_conv = cfg['first_conv']
    if isinstance(first_conv, str):
        first_conv = (first_conv,)
    assert isinstance(first_conv, (tuple, list))
    for fc in first_conv:
        assert fc + ".weight" in state_dict.keys(), f'{fc} not in model params'


if 'GITHUB_ACTIONS' not in os.environ:
    @pytest.mark.timeout(240)
    @pytest.mark.parametrize('model_name', list_models(pretrained=True))
    @pytest.mark.parametrize('batch_size', [1])
    def test_model_load_pretrained(model_name, batch_size):
        """Create that pretrained weights load, verify support for in_chans != 3 while doing so."""
        in_chans = 3 if 'pruned' in model_name else 1  # pruning not currently supported with in_chans change
        create_model(model_name, pretrained=True, in_chans=in_chans, num_classes=5)
        create_model(model_name, pretrained=True, in_chans=in_chans, num_classes=0)

    @pytest.mark.timeout(240)
    @pytest.mark.parametrize('model_name', list_models(pretrained=True, exclude_filters=NON_STD_FILTERS))
    @pytest.mark.parametrize('batch_size', [1])
    def test_model_features_pretrained(model_name, batch_size):
        """Create that pretrained weights load when features_only==True."""
        create_model(model_name, pretrained=True, features_only=True)


@pytest.mark.torchscript
@pytest.mark.timeout(timeout120)
@pytest.mark.parametrize(
    'model_name', list_models(exclude_filters=EXCLUDE_FILTERS + EXCLUDE_JIT_FILTERS, name_matches_cfg=True))
@pytest.mark.parametrize('batch_size', [1])
def test_model_forward_torchscript(model_name, batch_size):
    """Run a single forward pass with each model"""
    input_size = _get_input_size(model_name=model_name, target=TARGET_JIT_SIZE)
    if max(input_size) > MAX_JIT_SIZE:
        pytest.skip("Fixed input size model > limit.")

    with set_scriptable(True):
        model = create_model(model_name, pretrained=False)
    model.eval()

    model = torch.jit.script(model)
    model.to(torch_device)
    outputs = model(torch.randn((batch_size, *input_size)))

    assert outputs.shape[0] == batch_size
    assert not torch.isnan(outputs).any(), 'Output included NaNs'


EXCLUDE_FEAT_FILTERS = [
    '*pruned*',  # hopefully fix at some point
] + NON_STD_FILTERS
if 'GITHUB_ACTIONS' in os.environ:  # and 'Linux' in platform.system():
    # GitHub Linux runner is slower and hits memory limits sooner than MacOS, exclude bigger models
    EXCLUDE_FEAT_FILTERS += ['*resnext101_32x32d', '*resnext101_32x16d']


@pytest.mark.features
@pytest.mark.timeout(120)
@pytest.mark.parametrize('model_name', list_models(exclude_filters=EXCLUDE_FILTERS + EXCLUDE_FEAT_FILTERS))
@pytest.mark.parametrize('batch_size', [1])
def test_model_forward_features(model_name, batch_size):
    """Run a single forward pass with each model in feature extraction mode"""
    model = create_model(model_name, pretrained=False, features_only=True)
    model.eval()
    expected_channels = model.feature_info.channels()
    expected_reduction = model.feature_info.reduction()
    assert len(expected_channels) >= 3  # all models here should have at least 3 default feat levels

    input_size = _get_input_size(model=model, target=TARGET_FFEAT_SIZE)
    if max(input_size) > MAX_FFEAT_SIZE:
        pytest.skip("Fixed input size model > limit.")
    output_fmt = getattr(model, 'output_fmt', 'NCHW')
    feat_axis = get_channel_dim(output_fmt)
    spatial_axis = get_spatial_dim(output_fmt)
    import math

    outputs = model(torch.randn((batch_size, *input_size)))
    assert len(expected_channels) == len(outputs)
    spatial_size = input_size[-2:]
    for e, r, o in zip(expected_channels, expected_reduction, outputs):
        assert e == o.shape[feat_axis]
        assert o.shape[spatial_axis[0]] <= math.ceil(spatial_size[0] / r) + 1
        assert o.shape[spatial_axis[1]] <= math.ceil(spatial_size[1] / r) + 1
        assert o.shape[0] == batch_size
        assert not torch.isnan(o).any()


@pytest.mark.features
@pytest.mark.timeout(120)
@pytest.mark.parametrize('model_name', list_models(module=FEAT_INTER_FILTERS, exclude_filters=EXCLUDE_FILTERS + ['*pruned*']))
@pytest.mark.parametrize('batch_size', [1])
def test_model_forward_intermediates_features(model_name, batch_size):
    """Run a single forward pass with each model in feature extraction mode"""
    model = create_model(model_name, pretrained=False, features_only=True, feature_cls='getter')
    model.eval()
    expected_channels = model.feature_info.channels()
    expected_reduction = model.feature_info.reduction()

    input_size = _get_input_size(model=model, target=TARGET_FFEAT_SIZE)
    if max(input_size) > MAX_FFEAT_SIZE:
        pytest.skip("Fixed input size model > limit.")
    output_fmt = getattr(model, 'output_fmt', 'NCHW')
    feat_axis = get_channel_dim(output_fmt)
    spatial_axis = get_spatial_dim(output_fmt)
    import math

    outputs = model(torch.randn((batch_size, *input_size)))
    assert len(expected_channels) == len(outputs)
    spatial_size = input_size[-2:]
    for e, r, o in zip(expected_channels, expected_reduction, outputs):
        print(o.shape)
        assert e == o.shape[feat_axis]
        assert o.shape[spatial_axis[0]] <= math.ceil(spatial_size[0] / r) + 1
        assert o.shape[spatial_axis[1]] <= math.ceil(spatial_size[1] / r) + 1
        assert o.shape[0] == batch_size
        assert not torch.isnan(o).any()


@pytest.mark.features
@pytest.mark.timeout(120)
@pytest.mark.parametrize('model_name', list_models(module=FEAT_INTER_FILTERS, exclude_filters=EXCLUDE_FILTERS + ['*pruned*']))
@pytest.mark.parametrize('batch_size', [1])
def test_model_forward_intermediates(model_name, batch_size):
    """Run a single forward pass with each model in feature extraction mode"""
    model = create_model(model_name, pretrained=False)
    model.eval()
    feature_info = timm.models.FeatureInfo(model.feature_info, len(model.feature_info))
    expected_channels = feature_info.channels()
    expected_reduction = feature_info.reduction()
    assert len(expected_channels) >= 3  # all models here should have at least 3 feature levels

    input_size = _get_input_size(model=model, target=TARGET_FFEAT_SIZE)
    if max(input_size) > MAX_FFEAT_SIZE:
        pytest.skip("Fixed input size model > limit.")
    output_fmt = 'NCHW'  # NOTE output_fmt determined by forward_intermediates() arg, not model attribute
    feat_axis = get_channel_dim(output_fmt)
    spatial_axis = get_spatial_dim(output_fmt)
    import math

    inpt = torch.randn((batch_size, *input_size))
    output, intermediates = model.forward_intermediates(
        inpt,
        output_fmt=output_fmt,
    )
    assert len(expected_channels) == len(intermediates)
    spatial_size = input_size[-2:]
    for e, r, o in zip(expected_channels, expected_reduction, intermediates):
        assert e == o.shape[feat_axis]
        assert o.shape[spatial_axis[0]] <= math.ceil(spatial_size[0] / r) + 1
        assert o.shape[spatial_axis[1]] <= math.ceil(spatial_size[1] / r) + 1
        assert o.shape[0] == batch_size
        assert not torch.isnan(o).any()

    output2 = model.forward_features(inpt)
    assert torch.allclose(output, output2)

    # Test that grad-checkpointing, if supported
    try:
        model.set_grad_checkpointing()
    except:
        # throws if not supported, that's fine
        pass
    else:
        output3, _ = model.forward_intermediates(
            inpt,
            output_fmt=output_fmt,
        )
        assert torch.allclose(output, output3, rtol=1e-4, atol=1e-5), 'Output does not match'



def _create_fx_model(model, train=False):
    # This block of code does a bit of juggling to handle any case where there are multiple outputs in train mode
    # So we trace once and look at the graph, and get the indices of the nodes that lead into the original fx output
    # node. Then we use those indices to select from train_nodes returned by torchvision get_graph_node_names
    tracer_kwargs = dict(
        leaf_modules=get_notrace_modules(),
        autowrap_functions=get_notrace_functions(),
        #enable_cpatching=True,
        param_shapes_constant=True
    )
    train_nodes, eval_nodes = get_graph_node_names(model, tracer_kwargs=tracer_kwargs)

    eval_return_nodes = [eval_nodes[-1]]
    train_return_nodes = [train_nodes[-1]]
    if train:
        tracer = NodePathTracer(**tracer_kwargs)
        graph = tracer.trace(model)
        graph_nodes = list(reversed(graph.nodes))
        output_node_names = [n.name for n in graph_nodes[0]._input_nodes.keys()]
        graph_node_names = [n.name for n in graph_nodes]
        output_node_indices = [-graph_node_names.index(node_name) for node_name in output_node_names]
        train_return_nodes = [train_nodes[ix] for ix in output_node_indices]

    fx_model = create_feature_extractor(
        model,
        train_return_nodes=train_return_nodes,
        eval_return_nodes=eval_return_nodes,
        tracer_kwargs=tracer_kwargs,
    )
    return fx_model


EXCLUDE_FX_FILTERS = ['vit_gi*', 'hiera*']
# not enough memory to run fx on more models than other tests
if 'GITHUB_ACTIONS' in os.environ:
    EXCLUDE_FX_FILTERS += [
        'beit_large*',
        'mixer_l*',
        '*nfnet_f2*',
        '*resnext101_32x32d',
        'resnetv2_152x2*',
        'resmlp_big*',
        'resnetrs270',
        'swin_large*',
        'vgg*',
        'vit_large*',
        'vit_base_patch8*',
        'xcit_large*',
    ]


@pytest.mark.fxforward
@pytest.mark.timeout(120)
@pytest.mark.parametrize('model_name', list_models(exclude_filters=EXCLUDE_FILTERS + EXCLUDE_FX_FILTERS))
@pytest.mark.parametrize('batch_size', [1])
def test_model_forward_fx(model_name, batch_size):
    """
    Symbolically trace each model and run single forward pass through the resulting GraphModule
    Also check that the output of a forward pass through the GraphModule is the same as that from the original Module
    """
    if not has_fx_feature_extraction:
        pytest.skip("Can't test FX. Torch >= 1.10 and Torchvision >= 0.11 are required.")

    model = create_model(model_name, pretrained=False)
    model.eval()

    input_size = _get_input_size(model=model, target=TARGET_FWD_FX_SIZE)
    if max(input_size) > MAX_FWD_FX_SIZE:
        pytest.skip("Fixed input size model > limit.")
    with torch.inference_mode():
        inputs = torch.randn((batch_size, *input_size))
        outputs = model(inputs)
        if isinstance(outputs, tuple):
            outputs = torch.cat(outputs)

        model = _create_fx_model(model)
        fx_outputs = tuple(model(inputs).values())
        if isinstance(fx_outputs, tuple):
            fx_outputs = torch.cat(fx_outputs)

    assert torch.all(fx_outputs == outputs)
    assert outputs.shape[0] == batch_size
    assert not torch.isnan(outputs).any(), 'Output included NaNs'


@pytest.mark.fxbackward
@pytest.mark.timeout(120)
@pytest.mark.parametrize('model_name', list_models(
    exclude_filters=EXCLUDE_FILTERS + EXCLUDE_FX_FILTERS, name_matches_cfg=True))
@pytest.mark.parametrize('batch_size', [2])
def test_model_backward_fx(model_name, batch_size):
    """Symbolically trace each model and run single backward pass through the resulting GraphModule"""
    if not has_fx_feature_extraction:
        pytest.skip("Can't test FX. Torch >= 1.10 and Torchvision >= 0.11 are required.")

    input_size = _get_input_size(model_name=model_name, target=TARGET_BWD_FX_SIZE)
    if max(input_size) > MAX_BWD_FX_SIZE:
        pytest.skip("Fixed input size model > limit.")

    model = create_model(model_name, pretrained=False, num_classes=42)
    model.train()
    num_params = sum([x.numel() for x in model.parameters()])
    if 'GITHUB_ACTIONS' in os.environ and num_params > 100e6:
        pytest.skip("Skipping FX backward test on model with more than 100M params.")

    model = _create_fx_model(model, train=True)
    outputs = tuple(model(torch.randn((batch_size, *input_size))).values())
    if isinstance(outputs, tuple):
        outputs = torch.cat(outputs)
    outputs.mean().backward()
    for n, x in model.named_parameters():
        assert x.grad is not None, f'No gradient for {n}'
    num_grad = sum([x.grad.numel() for x in model.parameters() if x.grad is not None])

    assert outputs.shape[-1] == 42
    assert num_params == num_grad, 'Some parameters are missing gradients'
    assert not torch.isnan(outputs).any(), 'Output included NaNs'


if 'GITHUB_ACTIONS' not in os.environ:
    # FIXME this test is causing GitHub actions to run out of RAM and abruptly kill the test process

    # reason: model is scripted after fx tracing, but beit has torch.jit.is_scripting() control flow
    EXCLUDE_FX_JIT_FILTERS = [
        'deit_*_distilled_patch16_224',
        'levit*',
        'pit_*_distilled_224',
    ] + EXCLUDE_FX_FILTERS


    @pytest.mark.timeout(120)
    @pytest.mark.parametrize(
        'model_name', list_models(
            exclude_filters=EXCLUDE_FILTERS + EXCLUDE_JIT_FILTERS + EXCLUDE_FX_JIT_FILTERS, name_matches_cfg=True))
    @pytest.mark.parametrize('batch_size', [1])
    def test_model_forward_fx_torchscript(model_name, batch_size):
        """Symbolically trace each model, script it, and run single forward pass"""
        if not has_fx_feature_extraction:
            pytest.skip("Can't test FX. Torch >= 1.10 and Torchvision >= 0.11 are required.")

        input_size = _get_input_size(model_name=model_name, target=TARGET_JIT_SIZE)
        if max(input_size) > MAX_JIT_SIZE:
            pytest.skip("Fixed input size model > limit.")

        with set_scriptable(True):
            model = create_model(model_name, pretrained=False)
        model.eval()

        model = torch.jit.script(_create_fx_model(model))
        with torch.inference_mode():
            outputs = tuple(model(torch.randn((batch_size, *input_size))).values())
            if isinstance(outputs, tuple):
                outputs = torch.cat(outputs)

        assert outputs.shape[0] == batch_size
        assert not torch.isnan(outputs).any(), 'Output included NaNs'

    @pytest.mark.timeout(120)
    @pytest.mark.parametrize('model_name', ["regnetx_002"])
    @pytest.mark.parametrize('batch_size', [1])
    def test_model_forward_torchscript_with_features_fx(model_name, batch_size):
        """Create a model with feature extraction based on fx, script it, and run
        a single forward pass"""
        if not has_fx_feature_extraction:
            pytest.skip("Can't test FX. Torch >= 1.10 and Torchvision >= 0.11 are required.")

        allowed_models = list_models(
            exclude_filters=EXCLUDE_FILTERS + EXCLUDE_JIT_FILTERS + EXCLUDE_FX_JIT_FILTERS,
            name_matches_cfg=True
        )
        assert model_name in allowed_models, f"{model_name=} not supported for this test"

        input_size = _get_input_size(model_name=model_name, target=TARGET_JIT_SIZE)
        assert max(input_size) <= MAX_JIT_SIZE, "Fixed input size model > limit. Pick a different model to run this test"

        with set_scriptable(True):
            model = create_model(model_name, pretrained=False, features_only=True, feature_cfg={"feature_cls": "fx"})
        model.eval()

        model = torch.jit.script(model)
        with torch.inference_mode():
            outputs = model(torch.randn((batch_size, *input_size)))

        assert isinstance(outputs, list)

        for tensor in outputs:
            assert tensor.shape[0] == batch_size
            assert not torch.isnan(tensor).any(), 'Output included NaNs'
