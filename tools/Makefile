# Step1: Check Python version compatibility (3.6 <= x <= 3.9)
PYTHON_VERSION_MIN=3.6
PYTHON_VERSION_MAX=3.9
PYTHON_VERSION=$(shell python3 -c "import sys;t='{v[0]}.{v[1]}'.format(v=list(sys.version_info[:2]));sys.stdout.write(t)")
PYTHON_VERSION_OK=$(shell python3 -c 'import sys; print(int(float(${PYTHON_VERSION}) >= ${PYTHON_VERSION_MIN} and float(${PYTHON_VERSION}) <= ${PYTHON_VERSION_MAX}))')

#$(info $$PYTHON_VERSION_OK is [${PYTHON_VERSION_OK}])

ifeq ($(PYTHON_VERSION_OK),0)
  $(error "Need python $(PYTHON_VERSION) >= $(PYTHON_VERSION_MIN)")
endif
PYTHON_VERSION_WITHOUT_DOT = $(strip $(subst .,,$(PYTHON_VERSION)))

PYTHON_VERSION_38=$(shell python3 -c 'import sys; print(int(float(${PYTHON_VERSION}) >= 3.8))')

#$(info $$PYTHON_VERSION_38 is [${PYTHON_VERSION_38}])

ifeq ($(PYTHON_VERSION_38),0)
    PYTHON_VERSION_WITHOUT_DOT_M=$(PYTHON_VERSION_WITHOUT_DOT)m
else
    PYTHON_VERSION_WITHOUT_DOT_M=$(PYTHON_VERSION_WITHOUT_DOT)
endif

#$(info $$PYTHON_VERSION_WITHOUT_DOT_M is [${PYTHON_VERSION_WITHOUT_DOT_M}])

# End Step1

# Step2: Check CUDA version compatibility
CUDA_VERSION := 11.1
CUDA_VERSION_WITHOUT_DOT = $(strip $(subst .,,$(CUDA_VERSION)))

PYTORCH_VERSION := 1.8.1
TORCHVISION_VERSION := 0.9.1
TORCHAUDIO_VERSION := 0.8.1
# End Step2

.PHONY: all clean

all: activate torch

activate:
	# Install Python dependencies.
	pip install pip --upgrade; pip install -r requirements.txt; pip install matplotlib;

torch: activate
	pip install pip --upgrade; \
	\ # Install `torch`, `torch-optimizer`, `torchvision` and `torchaudio` based on CUDA version, PyTorch version and Python version.
        pip install https://download.pytorch.org/whl/cu$(CUDA_VERSION_WITHOUT_DOT)/torch-$(PYTORCH_VERSION)%2Bcu$(CUDA_VERSION_WITHOUT_DOT)-cp$(PYTHON_VERSION_WITHOUT_DOT)-cp$(PYTHON_VERSION_WITHOUT_DOT_M)-linux_x86_64.whl; pip install torch-optimizer; \
            pip install https://download.pytorch.org/whl/cu$(CUDA_VERSION_WITHOUT_DOT)/torchvision-$(TORCHVISION_VERSION)%2Bcu$(CUDA_VERSION_WITHOUT_DOT)-cp$(PYTHON_VERSION_WITHOUT_DOT)-cp$(PYTHON_VERSION_WITHOUT_DOT_M)-linux_x86_64.whl; \
            pip install https://download.pytorch.org/whl/torchaudio-${TORCHAUDIO_VERSION}-cp${PYTHON_VERSION_WITHOUT_DOT}-cp${PYTHON_VERSION_WITHOUT_DOT_M}-linux_x86_64.whl

clean:
	rm -fr venv
	find -iname "*.pyc" -delete
