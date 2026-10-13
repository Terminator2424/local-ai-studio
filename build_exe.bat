@echo off
rem Empaqueta Local AI Studio con PyInstaller (carpeta dist\LocalAIStudio). NO incluye PyTorch ni diffusers:
rem la generacion de imagenes y el sandbox de codigo requieren el entorno completo (run.bat).
chcp 65001 >nul
cd /d "%~dp0"
call venv\Scripts\activate.bat
pyinstaller --noconfirm --clean --name LocalAIStudio --windowed --onedir ^
  --collect-all customtkinter --collect-all tkinterweb --collect-all tkinterdnd2 --collect-data matplotlib ^
  --add-data "config.example.yaml;." ^
  --hidden-import local_ai_studio.ui.pdf_page --hidden-import local_ai_studio.ui.docs_page --hidden-import local_ai_studio.ui.chat_page ^
  --hidden-import local_ai_studio.ui.study_page --hidden-import local_ai_studio.ui.gen_page --hidden-import local_ai_studio.ui.edit_page ^
  --hidden-import local_ai_studio.ui.settings_page --hidden-import pynvml ^
  --exclude-module torch --exclude-module torchvision --exclude-module diffusers --exclude-module transformers ^
  --exclude-module accelerate --exclude-module pix2tex --exclude-module albumentations --exclude-module cv2 ^
  --exclude-module pandas --exclude-module IPython --exclude-module tensorboard --exclude-module bandit ^
  --exclude-module pip_audit --exclude-module pre_commit --exclude-module pytest ^
  app.py
