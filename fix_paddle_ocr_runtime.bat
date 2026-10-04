@echo off
setlocal
python -m pip uninstall -y paddlepaddle
python -m pip install --no-cache-dir paddlepaddle==3.1.0
python -c "import paddle,paddleocr; print('PaddlePaddle:', paddle.__version__); print('PaddleOCR:', paddleocr.__version__)"
pause
