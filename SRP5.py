#!/usr/bin/env python3
"""
Screen Recorder Pro - Professional Screen Recording Tool
Поддерживает запись экрана и системного звука с горячими клавишами
"""
import tkinter as tk
from tkinter import ttk, messagebox, Listbox, END, filedialog, simpledialog
import threading
import time
import os
import sys
import subprocess
import tempfile
import queue
import keyboard
import numpy as np
import wave
import cv2
from mss import mss
import wmi
import soundcard as sc
from datetime import datetime
import psutil
import pythoncom
import logging
import json
import traceback


# ================================
# НАСТРОЙКА ЛОГГИРОВАНИЯ
# ================================

class UnicodeFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            replacements = {
                '✅': '[OK]',
                '❌': '[ERROR]',
                '⚠️': '[WARNING]',
                '🎬': '[REC]',
                '🎵': '[AUDIO]',
                '🎧': '[AUDIO]',
                '🖥️': '[MONITOR]',
                '📢': '[NOTIFY]',
                '📁': '[FOLDER]',
                '🗑️': '[CLEAN]',
                '🔔': '[NOTIFY]',
                '⚙️': '[SETTINGS]',
                '🎯': '[QUALITY]',
                '💾': '[FORMAT]',
                '📹': '[VIDEO]',
                '🚀': '[START]',
                '⌨️': '[HOTKEYS]',
                '⏰': '[TIMER]',
                '🎨': '[GUI]',
                '🔴': '[REC]',
                '⚫': '[STOP]',
                '⏸️': '[PAUSE]',
                '▶️': '[RESUME]',
                '⏹️': '[STOP]',
                '🔧': '[TOOLS]',
                '⚡': '[QUICK]',
                '🎮': '[CONTROL]',
                '📊': '[STATS]',
                '🔍': '[FIND]',
                '✨': '[FEATURE]',
                '📋': '[CLIPBOARD]',
                '🔒': '[LOCK]',
                '📸': '[SNAPSHOT]',
                '🎥': '[CAMERA]',
                '💡': '[TIP]'
            }
            for emoji, text in replacements.items():
                record.msg = record.msg.replace(emoji, text)
        return True


def setup_logging():
    """Настройка логирования с проверкой доступности файла"""
    log_file = 'screen_recorder.log'

    try:
        if not os.path.exists(log_file):
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(f"Log file created at {datetime.now()}\n")
                f.write("Screen Recorder Pro v3.0\n")
                f.write("=" * 60 + "\n")
        else:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"\n\n{'=' * 60}\n")
                f.write(f"New session started at {datetime.now()}\n")
                f.write("=" * 60 + "\n")
    except Exception as e:
        print(f"⚠️ Не удалось создать/открыть файл лога: {e}")
        temp_dir = tempfile.gettempdir()
        log_file = os.path.join(temp_dir, 'screen_recorder.log')
        print(f"   Логи будут записываться в: {log_file}")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    logger = logging.getLogger(__name__)
    logger.addFilter(UnicodeFilter())

    logger.info(f"Запуск {Config.APP_NAME} v{Config.VERSION}")
    logger.info(f"Система: {sys.platform}, Python: {sys.version}")
    logger.info(f"Рабочая директория: {Config.APPLICATION_PATH}")

    return logger


# ================================
# КОНФИГУРАЦИЯ И ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ================================

class Config:
    """Конфигурация приложения"""
    APP_NAME = "SR Pro"
    VERSION = "3.0"
    AUTHOR = "Андрей"

    # Пути
    if getattr(sys, 'frozen', False):
        APPLICATION_PATH = os.path.dirname(sys.executable)
    else:
        APPLICATION_PATH = os.path.dirname(os.path.abspath(__file__))

    VIDEO_DIR = os.path.join(APPLICATION_PATH, "video")
    TEMP_DIR = os.path.join(APPLICATION_PATH, "video", "temp")
    SNAPSHOTS_DIR = os.path.join(APPLICATION_PATH, "snapshots")
    CONFIG_FILE = os.path.join(APPLICATION_PATH, "config.json")

    # Настройки по умолчанию
    DEFAULT_FPS = 30.0
    DEFAULT_FORMAT = "mp4"
    DEFAULT_AUDIO_DELAY = 0.0
    SAMPLE_RATE = 48000
    VIDEO_QUEUE_SIZE = 60
    AUDIO_BUFFER_SIZE = 4096


logger = setup_logging()


class AppState:
    recording = False
    paused = False
    start_event = None
    recording_start_time = None
    selected_monitor_index = 1
    audio_delay = Config.DEFAULT_AUDIO_DELAY
    audio_device_info = None
    audio_device_name = "Поиск аудиоустройств..."
    audio_data = []  # Данные с динамиков (системный звук)
    microphone_data = []  # Данные с микрофона
    video_queue = queue.Queue(maxsize=Config.VIDEO_QUEUE_SIZE)
    performance_stats = {"cpu_usage": 0, "memory_usage": 0, "disk_usage": 0}
    pending_notifications = []
    audio_thread_failed = False
    last_recording_file = None
    settings = {}
    preview_image = None
    audio_recording_method = "soundcard"
    selected_speaker_device = None  # Устройство для записи системного звука
    selected_microphone_device = None  # Устройство для записи микрофона
    record_speakers = True  # Записывать системный звук
    record_microphone = True  # Записывать микрофон

    # НОВЫЕ ПОЛЯ ДЛЯ СИНХРОНИЗАЦИИ
    common_start_time = None  # Общее время старта для всех потоков
    video_actual_start = None  # Фактическое время старта видео
    audio_actual_start = None  # Фактическое время старта аудио
    video_samples_recorded = 0  # Количество захваченных видео-кадров
    audio_samples_recorded = 0  # Количество записанных аудио-сэмплов
    recording_duration = 0  # Длительность записи


# Список описаний мониторов (заполняется при старте/создании интерфейса)
monitor_descriptions = []


# ================================
# УПРАВЛЕНИЕ НАСТРОЙКАМИ
# ================================

def load_settings():
    """Загружает настройки из файла"""
    default_settings = {
        "video_dir": Config.VIDEO_DIR,
        "auto_clear_temp": True,
        "show_notifications": True,
        "enable_hotkeys": True,
        "auto_open_folder": False,
        "selected_speaker_device": None,
        "selected_microphone_device": None,
        "record_speakers": True,
        "record_microphone": True
    }

    if os.path.exists(Config.CONFIG_FILE):
        try:
            with open(Config.CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                for key in default_settings:
                    if key in loaded:
                        default_settings[key] = loaded[key]
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек: {e}")

    AppState.settings = default_settings
    AppState.selected_speaker_device = default_settings.get('selected_speaker_device', None)
    AppState.selected_microphone_device = default_settings.get('selected_microphone_device', None)
    AppState.record_speakers = default_settings.get('record_speakers', True)
    AppState.record_microphone = default_settings.get('record_microphone', True)

    return default_settings


def save_settings():
    """Сохраняет настройки в файл"""
    try:
        # Обновляем настройки устройств
        AppState.settings['selected_speaker_device'] = AppState.selected_speaker_device
        AppState.settings['selected_microphone_device'] = AppState.selected_microphone_device
        AppState.settings['record_speakers'] = AppState.record_speakers
        AppState.settings['record_microphone'] = AppState.record_microphone

        with open(Config.CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(AppState.settings, f, indent=2, ensure_ascii=False)
        logger.info("Настройки сохранены")
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")
        return False


def ensure_directories():
    """Создает необходимые директории"""
    logger.info("Проверка необходимых папок...")

    dirs_to_create = [
        Config.VIDEO_DIR,
        Config.TEMP_DIR,
        Config.SNAPSHOTS_DIR
    ]

    for directory in dirs_to_create:
        try:
            os.makedirs(directory, exist_ok=True)
            logger.info(f"Папка создана/проверена: {os.path.basename(directory)}")
        except Exception as e:
            logger.error(f"Ошибка создания {directory}: {e}")

    if AppState.settings.get('video_dir') and AppState.settings['video_dir'] != Config.VIDEO_DIR:
        try:
            os.makedirs(AppState.settings['video_dir'], exist_ok=True)
        except:
            pass


def get_ffmpeg_path():
    """Определяет путь к FFmpeg - УПРОЩЕННАЯ ВЕРСИЯ"""
    if getattr(sys, 'frozen', False):
        # Режим exe (PyInstaller) - ffmpeg добавлен через --add-data "ffmpeg;ffmpeg"
        # В этом случае ffmpeg будет распакован во временную папку в подпапке 'ffmpeg'
        base_path = sys._MEIPASS  # Временная папка PyInstaller
        ffmpeg_path = os.path.join(base_path, "ffmpeg", "ffmpeg.exe")

        if os.path.exists(ffmpeg_path):
            logger.info(f"FFmpeg найден: {ffmpeg_path}")
            return ffmpeg_path
        else:
            # Если не нашли в ожидаемом месте, пробуем найти как просто ffmpeg.exe
            ffmpeg_path = os.path.join(base_path, "ffmpeg.exe")
            if os.path.exists(ffmpeg_path):
                logger.info(f"FFmpeg найден (альтернативный путь): {ffmpeg_path}")
                return ffmpeg_path

            # В крайнем случае - ищем в системном PATH
            logger.warning("FFmpeg не найден в пакете, ищем в системном PATH")
            return "ffmpeg"
    else:
        # Режим разработки (Python скрипт)
        # Просто используем системный ffmpeg (должен быть в PATH)
        return "ffmpeg"


def show_notification(text, error=False, warning=False):
    """Показывает уведомление в интерфейсе"""
    if not AppState.settings.get('show_notifications', True):
        return

    try:
        color = "red" if error else "orange" if warning else "#2e7d32"
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_text = f"[{timestamp}] {text}"

        if error:
            logger.error(f"Уведомление: {text}")
        elif warning:
            logger.warning(f"Уведомление: {text}")
        else:
            logger.info(f"Уведомление: {text}")

        if 'notification_listbox' in globals() and notification_listbox.winfo_exists():
            notification_listbox.insert(0, full_text)
            notification_listbox.itemconfig(0, {'foreground': color})

            if notification_listbox.size() > 15:
                notification_listbox.delete(15)

            notification_listbox.see(0)
        else:
            print(f"[NOTIFY] {full_text}")

    except Exception as e:
        logger.error(f"Ошибка показа уведомления: {e}")


def get_system_performance():
    """Возвращает текущую загрузку системы"""
    try:
        cpu_usage = psutil.cpu_percent(interval=0.1)
        memory_usage = psutil.virtual_memory().percent
        disk_usage = psutil.disk_usage(Config.VIDEO_DIR).percent if os.path.exists(Config.VIDEO_DIR) else 0

        AppState.performance_stats = {
            "cpu_usage": cpu_usage,
            "memory_usage": memory_usage,
            "disk_usage": disk_usage
        }
    except Exception as e:
        logger.error(f"Ошибка получения статистики системы: {e}")


def get_monitor_descriptions():
    """Получает список мониторов с подробным описанием"""
    descriptions = []
    try:
        with mss() as sct:
            monitors = sct.monitors[1:]

        try:
            c = wmi.WMI()
            monitor_info = {}
            for i, monitor in enumerate(c.Win32_DesktopMonitor(), 1):
                if monitor.Name:
                    monitor_info[i] = monitor.Name.strip()
                else:
                    monitor_info[i] = f"Монитор {i}"
        except:
            monitor_info = {}

        for i, mon in enumerate(monitors, 1):
            w, h = mon["width"], mon["height"]
            name = monitor_info.get(i, f"Монитор {i}")

            if i == 1:
                desc = f"🖥️ Основной экран ({w}×{h}) — {name}"
            else:
                desc = f"🖥️ Экран {i} ({w}×{h}) — {name}"

            descriptions.append(desc)

    except Exception as e:
        logger.error(f"Ошибка получения информации о мониторах: {e}")
        descriptions = ["🖥️ Основной экран"]

    return descriptions


def get_audio_devices():
    """Возвращает список доступных аудиоустройств"""
    devices = []
    try:
        # Получаем все микрофоны (включая loopback для системного звука)
        all_devices = sc.all_microphones(include_loopback=True)
        for device in all_devices:
            devices.append(device.name)
        logger.info(f"Найдено {len(devices)} аудиоустройств")
    except Exception as e:
        logger.error(f"Ошибка получения аудиоустройств: {e}")
        devices = ["Не удалось получить аудиоустройства"]

    return devices


def init_audio_devices():
    """Инициализирует аудиоустройства для записи звука"""
    try:
        logger.info("Инициализация аудиоустройств...")

        # Получаем все доступные устройства
        all_devices = sc.all_microphones(include_loopback=True)
        if not all_devices:
            AppState.audio_device_name = "❌ Аудиоустройства не найдены"
            logger.warning("Аудиоустройства не найдены")
            return False

        # Инициализируем устройства для системного звука (динамики)
        if AppState.record_speakers:
            speaker_device = None

            # Если есть сохраненное устройство для динамиков
            if AppState.selected_speaker_device:
                for device in all_devices:
                    if device.name == AppState.selected_speaker_device:
                        speaker_device = device
                        logger.info(f"Найдено сохраненное устройство для динамиков: {device.name}")
                        break

            # Если не нашли сохраненное, ищем loopback устройство
            if not speaker_device:
                for device in all_devices:
                    device_name = device.name.lower()
                    # Ищем loopback устройство для записи системного звука
                    if any(keyword in device_name for keyword in ['loopback', 'stereo mix', 'виртуальный',
                                                                  'what you hear', 'virtual', 'output', 'speaker',
                                                                  'динамик']):
                        speaker_device = device
                        logger.info(f"Найдено loopback устройство: {device.name}")
                        break

            # Если все еще не нашли, берем первое loopback устройство
            if not speaker_device:
                for device in all_devices:
                    if hasattr(device, 'is_loopback') and device.is_loopback:
                        speaker_device = device
                        logger.info(f"Найдено loopback устройство (по атрибуту): {device.name}")
                        break

            if speaker_device:
                AppState.selected_speaker_device = speaker_device.name
                logger.info(f"Устройство для записи динамиков: {speaker_device.name}")

        # Инициализируем устройство для микрофона
        if AppState.record_microphone:
            microphone_device = None

            # Если есть сохраненное устройство для микрофона
            if AppState.selected_microphone_device:
                for device in all_devices:
                    if device.name == AppState.selected_microphone_device:
                        microphone_device = device
                        logger.info(f"Найдено сохраненное устройство для микрофона: {device.name}")
                        break

            # Если не нашли сохраненное, ищем микрофон
            if not microphone_device:
                for device in all_devices:
                    device_name = device.name.lower()
                    # Ищем микрофон (не loopback)
                    if not any(keyword in device_name for keyword in ['loopback', 'stereo mix', 'виртуальный',
                                                                      'what you hear', 'virtual', 'output', 'speaker',
                                                                      'динамик']):
                        if any(keyword in device_name for keyword in
                               ['microphone', 'mic', 'микрофон', 'input', 'вход']):
                            microphone_device = device
                            logger.info(f"Найдено устройство микрофона: {device.name}")
                            break

            # Если все еще не нашли, берем первое устройство без loopback
            if not microphone_device:
                for device in all_devices:
                    if not any(keyword in device.name.lower() for keyword in ['loopback', 'stereo mix', 'виртуальный',
                                                                              'what you hear', 'virtual', 'output']):
                        microphone_device = device
                        logger.info(f"Найдено альтернативное устройство для микрофона: {device.name}")
                        break

            if microphone_device:
                AppState.selected_microphone_device = microphone_device.name
                logger.info(f"Устройство для записи микрофона: {microphone_device.name}")

        # Обновляем отображение
        update_audio_device_display()

        AppState.pending_notifications.append("Аудиоустройства инициализированы")
        return True

    except Exception as e:
        logger.error(f"Ошибка инициализации аудио: {e}", exc_info=True)
        AppState.audio_device_name = "❌ Ошибка инициализации аудио"
        return False


def update_audio_device_display():
    """Обновляет отображение выбранных аудиоустройств в интерфейсе"""
    if 'audio_status_label' in globals() and audio_status_label.winfo_exists():
        devices_text = []

        if AppState.record_speakers and AppState.selected_speaker_device:
            devices_text.append(f"🔊 Динамики: {AppState.selected_speaker_device[:30]}")

        if AppState.record_microphone and AppState.selected_microphone_device:
            devices_text.append(f"🎤 Микрофон: {AppState.selected_microphone_device[:30]}")

        if not devices_text:
            devices_text = ["Аудиоустройства не выбраны"]

        audio_status_label.config(text=" | ".join(devices_text))


def select_audio_device(device_type, device_name):
    """Выбирает конкретное аудиоустройство для записи"""
    try:
        logger.info(f"Выбор аудиоустройства: {device_type} = {device_name}")

        # Находим устройство по имени
        all_devices = sc.all_microphones(include_loopback=True)
        selected_device = None

        for device in all_devices:
            if device.name == device_name:
                selected_device = device
                break

        if selected_device:
            if device_type == 'speaker':
                AppState.selected_speaker_device = selected_device.name
                logger.info(f"Устройство для динамиков выбрано: {selected_device.name}")
                show_notification(f"Выбраны динамики: {selected_device.name[:30]}...")
            else:  # microphone
                AppState.selected_microphone_device = selected_device.name
                logger.info(f"Устройство для микрофона выбрано: {selected_device.name}")
                show_notification(f"Выбран микрофон: {selected_device.name[:30]}...")

            # Обновляем отображение
            update_audio_device_display()

            # Сохраняем настройки
            save_settings()
            return True
        else:
            logger.error(f"Устройство не найдено: {device_name}")
            show_notification(f"Устройство не найдено: {device_name}", error=True)
            return False

    except Exception as e:
        logger.error(f"Ошибка выбора аудиоустройства: {e}", exc_info=True)
        show_notification("Ошибка выбора аудиоустройства", error=True)
        return False


# ================================
# АУДИО ЗАПИСЬ
# ================================

def speaker_recording_thread(temp_speaker_path):
    """Поток записи системного звука (динамиков)"""
    thread_name = "SpeakerThread-soundcard"
    threading.current_thread().name = thread_name
    logger.info("Запуск потока записи системного звука через soundcard")
    pythoncom.CoInitialize()

    try:
        if not AppState.record_speakers or not AppState.selected_speaker_device:
            logger.info("Запись системного звука отключена")
            return

        AppState.audio_data = []
        AppState.audio_samples_recorded = 0  # Сбрасываем счетчик

        # Находим устройство
        all_devices = sc.all_microphones(include_loopback=True)
        speaker_device = None
        for device in all_devices:
            if device.name == AppState.selected_speaker_device:
                speaker_device = device
                break

        if not speaker_device:
            logger.error(f"Устройство для записи динамиков не найдено: {AppState.selected_speaker_device}")
            return

        logger.info(f"Запись системного звука с устройства: {speaker_device.name}")
        logger.info(f"Параметры: samplerate={Config.SAMPLE_RATE}, blocksize={Config.AUDIO_BUFFER_SIZE}")

        AppState.start_event.wait()

        # Записываем ВРЕМЯ ПОСЛЕ ожидания, когда поток действительно начал работу
        AppState.audio_actual_start = time.perf_counter()  # Используем perf_counter для точности

        logger.info(f"Аудио поток реально стартовал в: {AppState.audio_actual_start}")
        if AppState.common_start_time:
            logger.info(f"Время от общего старта: {AppState.audio_actual_start - AppState.common_start_time:.3f} сек")

        show_notification("Запись системного звука...")

        try:
            with speaker_device.recorder(samplerate=Config.SAMPLE_RATE, blocksize=Config.AUDIO_BUFFER_SIZE * 8) as mic:
                while AppState.recording:
                    if AppState.paused:
                        time.sleep(0.1)
                        continue

                    try:
                        data = mic.record(numframes=Config.AUDIO_BUFFER_SIZE)
                        if data is not None and len(data) > 0:
                            # Двойная проверка на паузу
                            if not AppState.paused:
                                AppState.audio_data.append(data.copy())
                                AppState.audio_samples_recorded += len(data)

                                # Логируем каждые 5 секунд записанного звука
                                if AppState.audio_samples_recorded % (Config.SAMPLE_RATE * 5) == 0:
                                    seconds_recorded = AppState.audio_samples_recorded / Config.SAMPLE_RATE
                                    logger.debug(f"Записано системного звука: {seconds_recorded:.1f} сек")
                            else:
                                logger.debug("Пропуск аудио данных из-за паузы")
                        else:
                            logger.warning("Получены пустые аудиоданные с динамиков")
                            time.sleep(0.01)

                    except Exception as e:
                        logger.error(f"Ошибка в цикле записи системного звука: {e}", exc_info=True)
                        time.sleep(0.01)
                        continue

            total_seconds = AppState.audio_samples_recorded / Config.SAMPLE_RATE
            logger.info(
                f"Запись системного звука завершена. Сэмплов: {AppState.audio_samples_recorded} ({total_seconds:.1f} сек)")

        except Exception as e:
            logger.error(f"Ошибка при открытии устройства динамиков: {e}", exc_info=True)
            show_notification("Не удалось открыть устройство динамиков", error=True)
            return

        # Сохраняем данные системного звука
        if AppState.audio_data and len(AppState.audio_data) > 0:
            try:
                logger.info(
                    f"Сохранение системного звука: {len(AppState.audio_data)} фреймов, {AppState.audio_samples_recorded} сэмплов")
                all_audio = np.concatenate(AppState.audio_data, axis=0)
                all_audio = np.clip(all_audio, -1.0, 1.0)
                audio_int16 = (all_audio * 32767).astype(np.int16)

                with wave.open(temp_speaker_path, 'wb') as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(Config.SAMPLE_RATE)
                    wf.writeframes(audio_int16.tobytes())

                logger.info(f"Системный звук сохранен: {temp_speaker_path} ({os.path.getsize(temp_speaker_path)} байт)")
                show_notification("Системный звук записан успешно ✅")

            except Exception as e:
                logger.error(f"Ошибка сохранения системного звука: {e}", exc_info=True)
                show_notification("Ошибка сохранения системного звука", error=True)
        else:
            logger.warning("Нет данных системного звука для сохранения")
            show_notification("Системный звук не записался", warning=True)

    except Exception as e:
        logger.error(f"Критическая ошибка в потоке записи системного звука: {e}", exc_info=True)
        show_notification("Критическая ошибка записи системного звука", error=True)
    finally:
        pythoncom.CoUninitialize()


def microphone_recording_thread(temp_microphone_path):
    """Поток записи звука с микрофона"""
    thread_name = "MicrophoneThread-soundcard"
    threading.current_thread().name = thread_name
    logger.info("Запуск потока записи микрофона через soundcard")
    pythoncom.CoInitialize()

    try:
        if not AppState.record_microphone or not AppState.selected_microphone_device:
            logger.info("Запись микрофона отключена")
            return

        AppState.microphone_data = []

        # Находим устройство
        all_devices = sc.all_microphones(include_loopback=True)
        microphone_device = None
        for device in all_devices:
            if device.name == AppState.selected_microphone_device:
                microphone_device = device
                break

        if not microphone_device:
            logger.error(f"Устройство для записи микрофона не найдено: {AppState.selected_microphone_device}")
            return

        logger.info(f"Запись микрофона с устройства: {microphone_device.name}")
        logger.info(f"Параметры: samplerate={Config.SAMPLE_RATE}, blocksize={Config.AUDIO_BUFFER_SIZE}")

        AppState.start_event.wait()

        show_notification("Запись микрофона...")

        try:
            with microphone_device.recorder(samplerate=Config.SAMPLE_RATE, blocksize=Config.AUDIO_BUFFER_SIZE) as mic:
                frames_recorded = 0
                while AppState.recording:
                    if AppState.paused:
                        time.sleep(0.1)
                        continue

                    try:
                        data = mic.record(numframes=Config.AUDIO_BUFFER_SIZE)
                        if data is not None and len(data) > 0:
                            # Двойная проверка на паузу
                            if not AppState.paused:
                                AppState.microphone_data.append(data.copy())
                                frames_recorded += 1
                                if frames_recorded % 100 == 0:
                                    logger.debug(f"Записано фреймов микрофона: {frames_recorded}")
                            else:
                                logger.debug("Пропуск микрофонных данных из-за паузы")
                        else:
                            logger.warning("Получены пустые данные с микрофона")
                            time.sleep(0.01)

                    except Exception as e:
                        logger.error(f"Ошибка в цикле записи микрофона: {e}", exc_info=True)
                        time.sleep(0.01)
                        continue

            logger.info(f"Запись микрофона завершена. Фреймов: {frames_recorded}")

        except Exception as e:
            logger.error(f"Ошибка при открытии устройства микрофона: {e}", exc_info=True)
            show_notification("Не удалось открыть устройство микрофона", error=True)
            return

        # Сохраняем данные микрофона
        if AppState.microphone_data and len(AppState.microphone_data) > 0:
            try:
                logger.info(f"Сохранение микрофона: {len(AppState.microphone_data)} фреймов")
                all_audio = np.concatenate(AppState.microphone_data, axis=0)
                all_audio = np.clip(all_audio, -1.0, 1.0)
                audio_int16 = (all_audio * 32767).astype(np.int16)

                with wave.open(temp_microphone_path, 'wb') as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(Config.SAMPLE_RATE)
                    wf.writeframes(audio_int16.tobytes())

                logger.info(f"Микрофон сохранен: {temp_microphone_path} ({os.path.getsize(temp_microphone_path)} байт)")
                show_notification("Микрофон записан успешно ✅")

            except Exception as e:
                logger.error(f"Ошибка сохранения микрофона: {e}", exc_info=True)
                show_notification("Ошибка сохранения микрофона", error=True)
        else:
            logger.warning("Нет данных микрофона для сохранения")
            show_notification("Микрофон не записался", warning=True)

    except Exception as e:
        logger.error(f"Критическая ошибка в потоке записи микрофона: {e}", exc_info=True)
        show_notification("Критическая ошибка записи микрофона", error=True)
    finally:
        pythoncom.CoUninitialize()


# ================================
# ВИДЕО ЗАПИСЬ
# ================================

def video_capture_thread(monitor):
    """Поток захвата видео с экрана"""
    threading.current_thread().name = "VideoCaptureThread"
    logger.info(f"Запуск потока захвата видео: монитор {AppState.selected_monitor_index}")

    with mss() as sct:
        AppState.start_event.wait()

        # Записываем ВРЕМЯ ПОСЛЕ ожидания
        AppState.video_actual_start = time.perf_counter()

        logger.info(f"Видео поток реально стартовал в: {AppState.video_actual_start}")
        if AppState.common_start_time:
            logger.info(f"Время от общего старта: {AppState.video_actual_start - AppState.common_start_time:.3f} сек")

        target_fps = Config.DEFAULT_FPS
        target_interval = 1.0 / target_fps
        next_capture_time = time.perf_counter() + target_interval
        frames_captured = 0

        logger.info(f"Начало захвата видео, FPS: {target_fps}")

        while AppState.recording:
            if AppState.paused:
                time.sleep(0.1)
                next_capture_time = time.perf_counter() + target_interval
                continue

            current_time = time.perf_counter()
            if current_time >= next_capture_time:
                try:
                    img = sct.grab(monitor)
                    frame = np.array(img)
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                    if not AppState.video_queue.full():
                        AppState.video_queue.put(frame_bgr)
                        frames_captured += 1
                        AppState.video_samples_recorded = frames_captured
                        if frames_captured % 30 == 0:
                            logger.debug(f"Захвачено видео-кадров: {frames_captured}")
                    else:
                        logger.warning("Очередь видео переполнена! Кадр пропущен.")

                    next_capture_time += target_interval
                    if next_capture_time < current_time:
                        next_capture_time = current_time + target_interval

                except Exception as e:
                    logger.error(f"Ошибка захвата кадра: {e}")

            else:
                time.sleep(0.001)

        total_seconds = frames_captured / target_fps
        logger.info(f"Захват завершен. Кадров захвачено: {frames_captured} ({total_seconds:.1f} сек)")


def video_writer_thread(temp_video_path, monitor):
    """Поток записи видео в файл"""
    threading.current_thread().name = "VideoWriterThread"
    logger.info(f"Запуск потока записи видео: {temp_video_path}")

    screen_width = monitor["width"]
    screen_height = monitor["height"]
    out_width, out_height = screen_width, screen_height

    fourcc_tests = [
        cv2.VideoWriter_fourcc(*'XVID'),
        cv2.VideoWriter_fourcc(*'MJPG'),
        cv2.VideoWriter_fourcc(*'I420'),
        cv2.VideoWriter_fourcc(*'YUY2'),
        -1
    ]

    writer = None
    fps = Config.DEFAULT_FPS

    for fourcc in fourcc_tests:
        try:
            if fourcc == -1:
                writer = cv2.VideoWriter(temp_video_path, fourcc, fps, (out_width, out_height))
            else:
                writer = cv2.VideoWriter(temp_video_path, fourcc, fps, (out_width, out_height))

            if writer is not None and writer.isOpened():
                logger.info(f"VideoWriter создан с кодеком: {fourcc if fourcc != -1 else 'системный'}")
                break
            elif writer is not None:
                writer.release()
        except:
            continue

    if writer is None or not writer.isOpened():
        logger.error(f"Не удалось создать VideoWriter для {temp_video_path}")
        show_notification("Ошибка создания видеофайла", error=True)
        return

    AppState.start_event.wait()
    frames_written = 0

    logger.info(f"Начало записи видео, разрешение: {out_width}x{out_height}, FPS: {fps}")

    while AppState.recording or not AppState.video_queue.empty():
        try:
            frame = AppState.video_queue.get(timeout=0.5)
            writer.write(frame)
            frames_written += 1
            if frames_written % 30 == 0:
                logger.debug(f"Записано видео-кадров: {frames_written}")
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Ошибка записи кадра: {e}")
            continue

    writer.release()
    logger.info(f"Запись видео завершена. Кадров записано: {frames_written}")


# ================================
# ОБРАБОТКА И СОХРАНЕНИЕ
# ================================

def merge_video_and_audio(video_file, speaker_file, microphone_file, timestamp):
    """Объединяет видео и аудио в конечный файл с индикацией прогресса"""
    ensure_directories()
    output_dir = AppState.settings.get('video_dir', Config.VIDEO_DIR)
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"record_{timestamp}.{Config.DEFAULT_FORMAT}")
    try:
        ffmpeg_path = get_ffmpeg_path()

        # === Проверка наличия аудиофайлов ===
        has_speaker_audio = False
        has_microphone_audio = False

        if speaker_file and os.path.exists(speaker_file) and AppState.record_speakers:
            try:
                speaker_file_size = os.path.getsize(speaker_file)
                if speaker_file_size > 1024:
                    with wave.open(speaker_file, 'rb') as wf:
                        frames = wf.getnframes()
                        if frames > 0:
                            has_speaker_audio = True
            except Exception as e:
                logger.warning(f"Файл динамиков поврежден или нечитаем: {e}")

        if microphone_file and os.path.exists(microphone_file) and AppState.record_microphone:
            try:
                microphone_file_size = os.path.getsize(microphone_file)
                if microphone_file_size > 1024:
                    with wave.open(microphone_file, 'rb') as wf:
                        frames = wf.getnframes()
                        if frames > 0:
                            has_microphone_audio = True
            except Exception as e:
                logger.warning(f"Файл микрофона поврежден или нечитаем: {e}")

        logger.info(
            f"Объединение: динамики={'есть' if has_speaker_audio else 'нет'}, микрофон={'есть' if has_microphone_audio else 'нет'}")

        # === Расчет задержки аудио ===
        audio_delay = 0.0
        delay_info = ""

        video_duration = 0.0
        audio_duration = 0.0

        # Получаем длительность видео через FFmpeg
        try:
            cmd = [ffmpeg_path, "-i", video_file, "-hide_banner"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            for line in result.stderr.split('\n'):
                if 'Duration:' in line:
                    duration_str = line.split('Duration:')[1].split(',')[0].strip()
                    h, m, s = duration_str.split(':')
                    video_duration = int(h) * 3600 + int(m) * 60 + float(s)
                    break
        except Exception as e:
            logger.error(f"Не удалось получить длительность видео: {e}")
            video_duration = AppState.recording_duration  # fallback

        # Получаем длительность аудио (если есть системный звук)
        if has_speaker_audio:
            try:
                with wave.open(speaker_file, 'rb') as wf:
                    audio_duration = wf.getnframes() / wf.getframerate()
            except Exception as e:
                logger.error(f"Ошибка чтения длительности аудио: {e}")

        # Расчёт задержки по времени старта потоков
        if (hasattr(AppState, 'video_actual_start') and AppState.video_actual_start and
            hasattr(AppState, 'audio_actual_start') and AppState.audio_actual_start):
            video_delay = AppState.video_actual_start - AppState.common_start_time
            audio_delay_start = AppState.audio_actual_start - AppState.common_start_time
            start_difference = audio_delay_start - video_delay
            audio_delay = start_difference  # в секундах

            if abs(audio_delay) > 0.001:
                frame_time = 1.0 / Config.DEFAULT_FPS
                audio_delay = round(audio_delay / frame_time) * frame_time
                if audio_delay > 0:
                    delay_info = f" (аудио отстает на {audio_delay:.3f} сек)"
                else:
                    delay_info = f" (аудио опережает на {abs(audio_delay):.3f} сек)"
                logger.info(f"Расчет задержки: {audio_delay:.3f} сек")

        # Автокоррекция на основе различия длительностей
        if has_speaker_audio and abs(video_duration - audio_duration) > 0.1:
            logger.warning(f"Разница длительностей: видео={video_duration:.2f}, аудио={audio_duration:.2f}")
            duration_diff = video_duration - audio_duration
            if duration_diff > 0:
                audio_delay += duration_diff / 2
                logger.info(f"Автокоррекция задержки: +{duration_diff / 2:.3f} сек")

        # === GUI прогресса ===
        progress_window = tk.Toplevel(root)
        progress_window.title("🎬 Обработка видео")
        progress_window.geometry("600x400")
        progress_window.resizable(False, False)
        progress_window.transient(root)
        progress_window.grab_set()
        x = root.winfo_x() + (root.winfo_width() - progress_window.winfo_width()) // 2
        y = root.winfo_y() + (root.winfo_height() - progress_window.winfo_height()) // 2
        progress_window.geometry(f"+{x}+{y}")

        title_label = ttk.Label(progress_window, text="⏳ Обработка видео", font=('Segoe UI', 14, 'bold'))
        title_label.pack(pady=(15, 5))

        info_label = ttk.Label(progress_window, text=f"Файл: record_{timestamp}.{Config.DEFAULT_FORMAT}")
        info_label.pack(pady=(0, 15))

        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar(progress_window, variable=progress_var, maximum=100, length=500, mode='indeterminate')
        progress_bar.pack(pady=10, padx=20)
        progress_bar.start(15)

        progress_status_label = ttk.Label(progress_window, text="Подготовка к обработке...")
        progress_status_label.pack(pady=5)

        time_label = ttk.Label(progress_window, text="Прошло: 0 сек")
        time_label.pack(pady=5)

        log_frame = ttk.Frame(progress_window)
        log_frame.pack(pady=10, padx=20, fill=tk.BOTH, expand=True)
        log_text = tk.Text(log_frame, height=8, width=70, wrap=tk.WORD, bg='#f0f0f0', font=('Consolas', 8))
        log_scrollbar = ttk.Scrollbar(log_frame, command=log_text.yview)
        log_text.config(yscrollcommand=log_scrollbar.set)
        log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        cancel_var = tk.BooleanVar(value=False)
        def cancel_processing():
            cancel_var.set(True)
            progress_status_label.config(text="🛑 Отмена запрошена...")
            log_text.insert(tk.END, ">>> Пользователь запросил отмену обработки\n")
            log_text.see(tk.END)
            cancel_button.config(state="disabled")

        cancel_button = ttk.Button(progress_window, text="Отменить обработку", command=cancel_processing, width=20)
        cancel_button.pack(pady=(5, 15))

        def append_log(message, is_error=False):
            log_timestamp = datetime.now().strftime("%H:%M:%S")
            log_text.insert(tk.END, f"[{log_timestamp}] {message}\n")
            if is_error:
                log_text.tag_add("error", "end-2l", "end-1l")
                log_text.tag_config("error", foreground="red")
            log_text.see(tk.END)
            progress_window.update()

        def set_determinate_progress():
            progress_bar.stop()
            progress_bar.config(mode='determinate')
            progress_var.set(0)

        # === Формирование команды FFmpeg ===
        cmd = [ffmpeg_path, "-y", "-loglevel", "info", "-i", video_file]
        audio_inputs = []
        complex_filter_parts = []
        audio_idx = 1

        if has_speaker_audio:
            cmd += ["-i", speaker_file]
            delay_ms = int(audio_delay * 1000)
            if delay_ms > 0:
                complex_filter_parts.append(f"[{audio_idx}:a]adelay={delay_ms}|{delay_ms}[speaker_audio]")
                append_log(f"🔊 Задержка динамиков: +{delay_ms}ms")
            else:
                complex_filter_parts.append(f"[{audio_idx}:a]anull[speaker_audio]")
                if delay_ms < 0:
                    append_log("🔊 Аудио опережает, компенсация не поддерживается напрямую")
            audio_inputs.append("speaker")
            audio_idx += 1

        if has_microphone_audio:
            cmd += ["-i", microphone_file]
            audio_inputs.append("microphone")
            audio_idx += 1

        # Настройки видео
        if AppState.recording_duration > 300:
            video_preset = "veryfast"
            video_crf = "28"
        else:
            video_preset = "medium"
            video_crf = "23"

        cmd += ["-c:v", "libx264", "-preset", video_preset, "-crf", video_crf, "-pix_fmt", "yuv420p", "-threads", "0"]

        # Настройки аудио
        if has_speaker_audio and has_microphone_audio:
            if complex_filter_parts:
                complex_filter_parts.append(f"[speaker_audio][{audio_idx - 1}:a]amix=inputs=2:duration=longest[a]")
                cmd += ["-filter_complex", ";".join(complex_filter_parts)]
                cmd += ["-map", "0:v", "-map", "[a]"]
            else:
                cmd += ["-filter_complex", "[1:a][2:a]amix=inputs=2:duration=longest[a]", "-map", "0:v", "-map", "[a]"]
            cmd += ["-c:a", "aac", "-b:a", "128k"]
            append_log("🔊 Микширование: динамики + микрофон" + delay_info)
        elif has_speaker_audio:
            if complex_filter_parts:
                cmd += ["-filter_complex", ";".join(complex_filter_parts), "-map", "0:v", "-map", "[speaker_audio]"]
            else:
                cmd += ["-map", "0:v", "-map", "1:a"]
            cmd += ["-c:a", "aac", "-b:a", "96k"]
            append_log("🔊 Только системный звук" + delay_info)
        elif has_microphone_audio:
            cmd += ["-c:a", "aac", "-b:a", "96k"]
            append_log("🎤 Только микрофон")
        else:
            cmd += ["-an"]
            append_log("🔇 Без звука")

        cmd += ["-movflags", "+faststart", output_file]

        # === Запуск FFmpeg в потоке ===
        def run_ffmpeg():
            start_time = time.time()
            process = None
            try:
                append_log("🚀 Запуск FFmpeg...")
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='ignore',
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )
                progress_window.after(100, set_determinate_progress)
                progress_status_label.config(text="Кодирование видео...")

                last_progress_update = 0
                while True:
                    if cancel_var.get():
                        process.terminate()
                        append_log("❌ Обработка отменена пользователем")
                        break
                    line = process.stderr.readline()
                    if not line and process.poll() is not None:
                        break
                    if line.strip():
                        if "time=" in line and "bitrate=" in line:
                            import re
                            time_match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                            if time_match:
                                hours, minutes, seconds = time_match.groups()
                                current_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                                if AppState.recording_duration > 0:
                                    progress_percent = min((current_seconds / AppState.recording_duration) * 100, 99)
                                    progress_var.set(progress_percent)
                                    if time.time() - last_progress_update > 5 or int(progress_percent) > int(last_progress_update):
                                        progress_status_label.config(
                                            text=f"Обработка: {progress_percent:.1f}% "
                                                 f"({int(current_seconds)}/{int(AppState.recording_duration)} сек)"
                                        )
                                        last_progress_update = progress_percent
                                elapsed = int(time.time() - start_time)
                                time_label.config(text=f"Прошло: {elapsed} сек")
                        if any(kw in line.lower() for kw in ["error", "warning", "frame=", "bitrate=", "speed="]):
                            append_log(line.strip())

                returncode = process.wait()
                total_time = int(time.time() - start_time)

                if returncode == 0 and os.path.exists(output_file):
                    file_size = os.path.getsize(output_file) / (1024 * 1024)
                    progress_var.set(100)
                    progress_status_label.config(text=f"✅ Готово! ({file_size:.1f} MB, {total_time} сек)")
                    append_log(f"✅ Файл успешно создан: {file_size:.1f} MB")
                    AppState.last_recording_file = output_file
                    time.sleep(1)
                    progress_window.destroy()
                    root.after(0, lambda: show_notification(f"Сохранено: {os.path.basename(output_file)} ({file_size:.1f} MB) ✅"))
                else:
                    progress_status_label.config(text="❌ Ошибка обработки")
                    cancel_button.config(text="Закрыть", command=progress_window.destroy)
                    append_log(f"❌ Ошибка FFmpeg (код {returncode})", is_error=True)
                    root.after(0, lambda: show_notification("Ошибка при создании файла", error=True))
            except Exception as e:
                append_log(f"❌ Исключение: {e}", is_error=True)
                if process:
                    process.terminate()
                error_text = str(e)[:50]
                root.after(0, lambda msg=error_text: show_notification(f"Ошибка обработки: {msg}...", error=True))

        threading.Thread(target=run_ffmpeg, daemon=True).start()
        progress_window.wait_window()

    except Exception as e:
        logger.error(f"Общая ошибка при объединении: {e}", exc_info=True)
        show_notification("Не удалось создать итоговый файл", error=True)

def get_video_duration(video_path):
    """Получает длительность видео файла"""
    try:
        ffmpeg_path = get_ffmpeg_path()
        cmd = [ffmpeg_path, "-i", video_path, "-hide_banner"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

        for line in result.stderr.split('\n'):
            if 'Duration:' in line:
                duration_str = line.split('Duration:')[1].split(',')[0].strip()
                parts = duration_str.split(':')
                if len(parts) == 3:
                    hours = int(parts[0])
                    minutes = int(parts[1])
                    seconds = float(parts[2])

                    if hours > 0:
                        return f"{hours}ч {minutes}м {int(seconds)}с"
                    elif minutes > 0:
                        return f"{minutes}м {int(seconds)}с"
                    else:
                        return f"{int(seconds)}с"
    except:
        pass
    return None


def safe_remove_file(filepath, max_attempts=5, delay=0.2):
    """Безопасно удаляет файл с повторными попытками и валидацией"""
    if not filepath or not isinstance(filepath, str):
        logger.warning("Попытка удалить некорректный путь к файлу")
        return
    
    filepath = os.path.abspath(filepath)
    
    # Проверка безопасности пути
    allowed_dirs = [Config.TEMP_DIR, AppState.settings.get('video_dir', Config.VIDEO_DIR)]
    if not any(filepath.startswith(os.path.abspath(d)) for d in allowed_dirs):
        logger.error(f"Попытка удалить файл вне разрешенных директорий: {filepath}")
        return
    
    if not os.path.exists(filepath):
        logger.debug(f"Файл уже не существует: {filepath}")
        return

    for attempt in range(max_attempts):
        try:
            # Проверяем размер файла перед удалением
            file_size = os.path.getsize(filepath)
            os.remove(filepath)
            logger.info(f"Файл удален: {filepath} ({file_size} байт)")
            return
        except PermissionError:
            if attempt < max_attempts - 1:
                logger.warning(f"Попытка {attempt + 1}/{max_attempts}: файл занят {filepath}")
                time.sleep(delay)
            else:
                logger.error(f"Не удалось удалить файл после {max_attempts} попыток: {filepath}")
        except FileNotFoundError:
            logger.debug(f"Файл уже удален другой операцией: {filepath}")
            return
        except Exception as e:
            logger.error(f"Неожиданная ошибка при удалении файла {filepath}: {e}")
            break


def validate_file_path(filepath, allowed_extensions=None):
    """Валидирует путь к файлу для безопасности"""
    if not filepath or not isinstance(filepath, str):
        return False
    
    try:
        filepath = os.path.abspath(filepath)
        
        # Проверка на traversal атаку
        if '..' in filepath:
            return False
        
        # Проверка расширения
        if allowed_extensions:
            ext = os.path.splitext(filepath)[1].lower()
            if ext not in allowed_extensions:
                return False
        
        # Проверка разрешенных директорий
        allowed_dirs = [
            Config.VIDEO_DIR,
            Config.TEMP_DIR,
            Config.SNAPSHOTS_DIR,
            AppState.settings.get('video_dir', Config.VIDEO_DIR)
        ]
        
        return any(filepath.startswith(os.path.abspath(d)) for d in allowed_dirs)
    
    except Exception:
        return False


def safe_open_file(filepath):
    """Безопасно открывает файл с валидацией"""
    if not os.path.exists(filepath):
        show_notification("Файл не найден ⚠️", warning=True)
        return False
    
    if not validate_file_path(filepath, ['.mp4', '.mkv', '.webm', '.avi', '.png', '.jpg', '.jpeg']):
        show_notification("Небезопасный путь к файлу", error=True)
        return False
    
    try:
        if sys.platform == "win32":
            os.startfile(filepath)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", filepath])
        else:
            subprocess.Popen(["xdg-open", filepath])
        return True
    except Exception as e:
        logger.error(f"Ошибка открытия файла {filepath}: {e}")
        show_notification(f"Не удалось открыть файл: {os.path.basename(filepath)}", error=True)
        return False


def start_recording():
    """Запускает запись экрана и звука"""

    if AppState.recording:
        show_notification("Запись уже идет ⚠️", warning=True)
        return

    AppState.recording_start_time = None  # Сбросить
    AppState.video_actual_start = None
    AppState.audio_actual_start = None

    ensure_directories()

    try:
        stat = os.statvfs(Config.VIDEO_DIR)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < 1.0:
            response = messagebox.askyesno("Мало места",
                                           f"На диске осталось только {free_gb:.1f} ГБ свободного места.\nПродолжить запись?")
            if not response:
                return
    except:
        pass

    logger.info(f"Начало записи: динамики={AppState.record_speakers}, микрофон={AppState.record_microphone}")

    AppState.audio_thread_failed = False
    AppState.recording = True
    AppState.paused = False
    AppState.recording_start_time = time.time()
    AppState.start_event = threading.Event()
    AppState.common_start_time = None

    try:
        with mss() as sct:
            monitor = sct.monitors[AppState.selected_monitor_index]
        logger.info(f"Выбран монитор {AppState.selected_monitor_index}: {monitor['width']}x{monitor['height']}")
    except Exception as e:
        logger.error(f"Ошибка получения монитора: {e}", exc_info=True)
        show_notification("Ошибка выбора монитора", error=True)
        AppState.recording = False
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_video = os.path.join(Config.TEMP_DIR, f"temp_video_{timestamp}.avi")
    temp_speaker = os.path.join(Config.TEMP_DIR, f"temp_speaker_{timestamp}.wav") if AppState.record_speakers else None
    temp_microphone = os.path.join(Config.TEMP_DIR,
                                   f"temp_microphone_{timestamp}.wav") if AppState.record_microphone else None

    logger.info(f"Временные файлы: видео={temp_video}, динамики={temp_speaker}, микрофон={temp_microphone}")

    # Сбрасываем длительность записи
    AppState.recording_duration = 0

    threads = []

    # Создаем потоки для записи аудио
    if AppState.record_speakers and AppState.selected_speaker_device:
        speaker_thread = threading.Thread(
            target=speaker_recording_thread,
            args=(temp_speaker,),
            daemon=True
        )
        threads.append(speaker_thread)
        logger.info("Поток записи системного звука создан")

    if AppState.record_microphone and AppState.selected_microphone_device:
        microphone_thread = threading.Thread(
            target=microphone_recording_thread,
            args=(temp_microphone,),
            daemon=True
        )
        threads.append(microphone_thread)
        logger.info("Поток записи микрофона создан")

    # Потоки для видео
    capture_thread = threading.Thread(
        target=video_capture_thread,
        args=(monitor,),
        daemon=True
    )

    writer_thread = threading.Thread(
        target=video_writer_thread,
        args=(temp_video, monitor),
        daemon=True
    )

    threads.extend([capture_thread, writer_thread])

    for thread in threads:
        thread.start()
        logger.debug(f"Поток запущен: {thread.name}")

    # Даем всем потокам время инициализироваться
    time.sleep(0.1)

    # СИНХРОННЫЙ СТАРТ ВСЕХ ПОТОКОВ
    AppState.common_start_time = time.perf_counter()
    AppState.start_event.set()

    logger.info(f"Все потоки запущены, синхронный старт в: {AppState.common_start_time}")

    start_button.config(state="disabled")
    stop_button.config(state="normal")
    pause_button.config(state="normal", text="⏸️ Пауза [F11]")
    status_label.config(text="● Запись...", foreground="#d32f2f")

    start_button.configure(style="Recording.TButton")
    stop_button.configure(style="Normal.TButton")
    pause_button.configure(style="Normal.TButton")

    # Показываем информацию о записываемых источниках звука
    audio_sources = []
    if AppState.record_speakers:
        audio_sources.append("динамики")
    if AppState.record_microphone:
        audio_sources.append("микрофон")

    audio_info = f" ({'+'.join(audio_sources)})" if audio_sources else " (без звука)"
    show_notification(f"Запись начата{audio_info} 🎬")
    logger.info("Запись начата успешно")

    def wait_for_completion():
        """Ожидание завершения записи с параллельной обработкой"""
        # Ждем пока все потоки действительно стартовали
        time.sleep(0.2)

        # Логируем времена старта для диагностики
        if hasattr(AppState, 'video_actual_start') and hasattr(AppState, 'audio_actual_start'):
            if AppState.video_actual_start and AppState.audio_actual_start:
                delay = AppState.video_actual_start - AppState.audio_actual_start
                logger.info(f"Время старта видео: {AppState.video_actual_start}")
                logger.info(f"Время старта аудио: {AppState.audio_actual_start}")
                logger.info(f"Разница (видео-аудио): {delay:.3f} сек")
                if abs(delay) > 0.05:  # Если задержка больше 50ms
                    logger.warning(f"Большая задержка между стартом видео и аудио: {delay:.3f} сек")

        while AppState.recording:
            time.sleep(0.1)

        logger.info("Запись остановлена, ожидание завершения потоков...")

        # Сохраняем длительность записи для прогресса
        if AppState.recording_start_time:
            AppState.recording_duration = time.time() - AppState.recording_start_time
            logger.info(f"Длительность записи: {AppState.recording_duration:.1f} сек")

        # Даем потокам немного времени на завершение
        time.sleep(0.5)

        # СРАЗУ запускаем объединение, не дожидаясь полного завершения всех потоков
        merge_video_and_audio(temp_video, temp_speaker, temp_microphone, timestamp)

        # В фоновом режиме очищаем временные файлы (через 10 секунд)
        def cleanup_temp_files():
            time.sleep(10)  # Даем время FFmpeg обработать файлы
            if AppState.settings.get('auto_clear_temp', True):
                safe_remove_file(temp_video)
                if temp_speaker:
                    safe_remove_file(temp_speaker)
                if temp_microphone:
                    safe_remove_file(temp_microphone)
                logger.info("Временные файлы очищены")

        threading.Thread(target=cleanup_temp_files, daemon=True).start()

        # Обновляем интерфейс
        def update_ui_after_recording():
            try:
                start_button.config(state="normal")
                stop_button.config(state="disabled")
                pause_button.config(state="disabled", text="⏸️ Пауза [F11]")
                status_label.config(text="✅ Обработка...", foreground="#388e3c")

                start_button.configure(style="Normal.TButton")
                stop_button.configure(style="Disabled.TButton")
                pause_button.configure(style="Disabled.TButton")

                update_video_list()
            except Exception as e:
                logger.error(f"Ошибка обновления UI: {e}")

        root.after(0, update_ui_after_recording)

        logger.info("Запись передана на обработку")

    threading.Thread(target=wait_for_completion, daemon=True).start()


def stop_recording():
    """Останавливает запись"""
    logger.info("Остановка записи по запросу пользователя")
    AppState.recording = False
    AppState.paused = False
    show_notification("Запись остановлена ⏹️")


def toggle_pause():
    """Переключает режим паузы"""
    if not AppState.recording:
        return

    AppState.paused = not AppState.paused
    if AppState.paused:
        pause_button.config(text="▶️ Возобновить [F11]")
        status_label.config(text="⏸️ Пауза", foreground="#f57c00")
        show_notification("Запись приостановлена")
        logger.info("Запись приостановлена")
    else:
        pause_button.config(text="⏸️ Пауза [F11]")
        status_label.config(text="● Запись...", foreground="#d32f2f")
        show_notification("Запись возобновлена")
        logger.info("Запись возобновлена")


# ================================
# НОВЫЕ ФУНКЦИИ ДЛЯ УДОБСТВА
# ================================

def take_screenshot():
    """Создает скриншот текущего экрана"""
    try:
        with mss() as sct:
            monitor = sct.monitors[AppState.selected_monitor_index]
            img = sct.grab(monitor)
            frame = np.array(img)
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(Config.SNAPSHOTS_DIR, f"screenshot_{timestamp}.png")

            cv2.imwrite(screenshot_path, cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR))
            file_size = os.path.getsize(screenshot_path)

            logger.info(f"Скриншот сохранен: {screenshot_path} ({file_size} байт)")
            show_notification(f"Скриншот сохранен: {os.path.basename(screenshot_path)} 📸")

    except Exception as e:
        logger.error(f"Ошибка создания скриншота: {e}", exc_info=True)
        show_notification("Ошибка создания скриншота", error=True)


def open_last_recording():
    """Открывает последнюю запись"""
    if AppState.last_recording_file and os.path.exists(AppState.last_recording_file):
        if safe_open_file(AppState.last_recording_file):
            show_notification(f"Открыта последняя запись: {os.path.basename(AppState.last_recording_file)}")
    else:
        logger.warning("Нет последней записи для открытия")
        show_notification("Нет последней записи для открытия ⚠️", warning=True)


def copy_filepath_to_clipboard():
    """Копирует путь к выбранному файлу в буфер обмена"""
    selection = video_listbox.curselection()
    if not selection:
        show_notification("Выберите файл из списка ⚠️", warning=True)
        return

    line = video_listbox.get(selection[0])
    try:
        if " — " in line:
            filename = line.split(" — ", 1)[1].split(" (", 1)[0]
        else:
            filename = line

        filepath = os.path.join(AppState.settings.get('video_dir', Config.VIDEO_DIR), filename.strip())

        root.clipboard_clear()
        root.clipboard_append(filepath)
        root.update()

        logger.info(f"Путь скопирован в буфер обмена: {filepath}")
        show_notification("Путь скопирован в буфер обмена 📋")
    except Exception as e:
        logger.error(f"Ошибка копирования пути: {e}", exc_info=True)
        show_notification("Ошибка копирования пути", error=True)


def rename_selected_file():
    """Переименовывает выбранный файл"""
    selection = video_listbox.curselection()
    if not selection:
        show_notification("Выберите файл из списка ⚠️", warning=True)
        return

    line = video_listbox.get(selection[0])
    try:
        if " — " in line:
            filename = line.split(" — ", 1)[1].split(" (", 1)[0]
        else:
            filename = line

        old_path = os.path.join(AppState.settings.get('video_dir', Config.VIDEO_DIR), filename.strip())

        if not os.path.exists(old_path):
            show_notification("Файл не найден ⚠️", warning=True)
            return

        new_name = simpledialog.askstring("Переименование",
                                          "Введите новое имя файла (без расширения):",
                                          initialvalue=os.path.splitext(filename)[0])

        if new_name and new_name.strip():
            new_name = new_name.strip()
            if not new_name.endswith('.mp4'):
                new_name += '.mp4'

            new_path = os.path.join(os.path.dirname(old_path), new_name)

            if os.path.exists(new_path):
                if not messagebox.askyesno("Файл существует",
                                           "Файл с таким именем уже существует. Перезаписать?"):
                    return

            os.rename(old_path, new_path)
            logger.info(f"Файл переименован: {old_path} -> {new_path}")
            show_notification(f"Файл переименован: {new_name}")
            update_video_list()

    except Exception as e:
        logger.error(f"Ошибка переименования файла: {e}", exc_info=True)
        show_notification("Ошибка переименования файла", error=True)


def open_folder(folder_path=None):
    """Открывает папку в проводнике"""
    if not folder_path:
        folder_path = AppState.settings.get('video_dir', Config.VIDEO_DIR)

    try:
        ensure_directories()
        logger.info(f"Открытие папки: {folder_path}")
        if sys.platform == "win32":
            os.startfile(folder_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder_path])
        else:
            subprocess.Popen(["xdg-open", folder_path])
        show_notification(f"Открыта папка: {os.path.basename(folder_path)}")
    except Exception as e:
        logger.error(f"Ошибка открытия папки: {e}", exc_info=True)
        show_notification(f"Не удалось открыть папку: {e}", error=True)


def clear_temp_folder():
    """Очищает папку с временными файлами"""
    try:
        deleted_count = 0
        total_size = 0
        for filename in os.listdir(Config.TEMP_DIR):
            if filename.startswith("temp_"):
                filepath = os.path.join(Config.TEMP_DIR, filename)
                try:
                    file_size = os.path.getsize(filepath)
                    os.remove(filepath)
                    deleted_count += 1
                    total_size += file_size
                    logger.debug(f"Удален временный файл: {filename} ({file_size} байт)")
                except:
                    logger.warning(f"Не удалось удалить временный файл: {filename}")

        if deleted_count > 0:
            size_mb = total_size / (1024 * 1024)
            logger.info(f"Удалено {deleted_count} временных файлов ({size_mb:.1f} MB)")
            show_notification(f"Удалено {deleted_count} файлов ({size_mb:.1f} MB) 🗑️")
        else:
            logger.info("Временных файлов нет")
            show_notification("Временных файлов нет ✅")

    except Exception as e:
        logger.error(f"Ошибка очистки temp: {e}", exc_info=True)
        show_notification(f"Ошибка очистки temp: {e}", error=True)


def debug_audio_devices():
    """Диагностика аудиоустройств"""
    try:
        logger.info("Запуск диагностики аудиоустройств")

        try:
            mics = sc.all_microphones(include_loopback=True)
            logger.info(f"Soundcard: найдено {len(mics)} устройств")
            for i, mic in enumerate(mics):
                logger.info(f"  {i + 1}. {mic.name}")
                try:
                    with mic.recorder(samplerate=44100, blocksize=1024) as test_mic:
                        test_mic.record(numframes=1024)
                    logger.info(f"      Доступно: ДА, каналы: {mic.channels if hasattr(mic, 'channels') else '?'}")
                except Exception as e:
                    logger.info(f"      Доступно: НЕТ, ошибка: {str(e)[:50]}")
        except Exception as e:
            logger.error(f"Soundcard диагностика не удалась: {e}")

        show_notification("Диагностика аудио завершена, см. логи")

    except Exception as e:
        logger.error(f"Ошибка диагностики аудио: {e}", exc_info=True)
        show_notification("Ошибка диагностики аудио", error=True)


def check_audio_device_ui(device_name, device_type="speaker"):
    """Тестирует конкретное аудиоустройство с визуальной обратной связью"""
    test_window = tk.Toplevel(root)
    test_window.title(f"🎵 Тестирование {device_name}")
    test_window.geometry("500x350")
    test_window.resizable(False, False)
    test_window.transient(root)
    test_window.grab_set()
    
    # Центрируем окно
    test_window.update_idletasks()
    x = root.winfo_x() + (root.winfo_width() - test_window.winfo_width()) // 2
    y = root.winfo_y() + (root.winfo_height() - test_window.winfo_height()) // 2
    test_window.geometry(f"+{x}+{y}")
    
    # Создаем фрейм для тестирования
    main_frame = ttk.Frame(test_window, padding="20")
    main_frame.pack(fill='both', expand=True)
    
    # Информация об устройстве
    ttk.Label(main_frame, text="Тестирование устройства:", font=('Segoe UI', 10)).pack(anchor='w')
    ttk.Label(main_frame, text=device_name[:50] + "..." if len(device_name) > 50 else device_name, 
              font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(0, 15))
    
    # Статус теста
    status_label = ttk.Label(main_frame, text="🔄 Подготовка к тестированию...", 
                             font=('Segoe UI', 11), foreground='#1976d2')
    status_label.pack(anchor='w', pady=10)
    
    # Прогресс-бар
    progress_frame = ttk.Frame(main_frame)
    progress_frame.pack(fill='x', pady=10)
    
    progress_bar = ttk.Progressbar(progress_frame, mode='indeterminate', length=400)
    progress_bar.pack(side='left', padx=(0, 10))
    
    # Детали теста
    details_text = tk.Text(main_frame, height=8, width=55, font=('Consolas', 9), 
                           bg='#f5f5f5', relief='flat', wrap='word')
    details_text.pack(fill='both', expand=True, pady=10)
    details_text.insert('1.0', "Ожидание начала теста...\n")
    details_text.config(state='disabled')
    
    # Кнопки
    button_frame = ttk.Frame(main_frame)
    button_frame.pack(fill='x', pady=(10, 0))
    
    def close_test():
        test_window.destroy()
    
    close_button = ttk.Button(button_frame, text="Закрыть", command=close_test)
    close_button.pack(side='right')
    
    # Функция обновления статуса
    def update_status(text, color='#1976d2'):
        status_label.config(text=text, foreground=color)
        test_window.update_idletasks()
    
    def add_details(text):
        details_text.config(state='normal')
        details_text.insert('end', text + '\n')
        details_text.see('end')
        details_text.config(state='disabled')
        test_window.update_idletasks()
    
    def run_test():
        try:
            update_status("🔍 Поиск устройства...", '#1976d2')
            progress_bar.start(10)
            add_details(f"Начало теста: {datetime.now().strftime('%H:%M:%S')}")
            add_details(f"Устройство: {device_name}")
            add_details(f"Тип: {device_type}")
            add_details("-" * 50)
            
            # Ищем устройство
            all_devices = sc.all_microphones(include_loopback=True)
            target_device = None
            
            for device in all_devices:
                if device.name == device_name:
                    target_device = device
                    break
            
            if not target_device:
                update_status("❌ Устройство не найдено", '#d32f2f')
                progress_bar.stop()
                add_details("ОШИБКА: Устройство не найдено в списке доступных")
                return
            
            add_details("✅ Устройство найдено")
            
            # Тест 1: Базовая доступность
            update_status("🧪 Тест 1: Проверка доступности...", '#1976d2')
            add_details("Тест 1: Проверка доступности...")
            
            try:
                with target_device.recorder(samplerate=Config.SAMPLE_RATE, blocksize=1024) as recorder:
                    pass
                add_details("✅ Устройство доступно для записи")
            except Exception as e:
                update_status("❌ Устройство недоступно", '#d32f2f')
                progress_bar.stop()
                add_details(f"❌ ОШИБКА: {str(e)}")
                return
            
            # Тест 2: Запись короткого фрагмента
            update_status("🎙️ Тест 2: Запись тестового фрагмента...", '#1976d2')
            add_details("Тест 2: Запись тестового фрагмента (3 секунды)...")
            
            test_data = []
            test_duration = 3.0  # 3 секунды
            sample_rate = Config.SAMPLE_RATE
            block_size = 1024
            blocks_to_record = int((test_duration * sample_rate) / block_size)
            
            try:
                with target_device.recorder(samplerate=sample_rate, blocksize=block_size) as recorder:
                    update_status("🎙️ Идет запись...", '#ff9800')
                    
                    for i in range(blocks_to_record):
                        try:
                            data = recorder.record(numframes=block_size)
                            if data is not None and len(data) > 0:
                                # Проверяем тип данных и конвертируем если нужно
                                if hasattr(data, 'astype'):
                                    # Это уже numpy массив
                                    test_data.append(data)
                                else:
                                    # Конвертируем в numpy массив
                                    test_data.append(np.array(data))
                            else:
                                logger.warning(f"Получены пустые данные на итерации {i}")
                        except Exception as record_error:
                            logger.error(f"Ошибка записи фрагмента {i}: {record_error}")
                            continue
                        
                        # Обновляем прогресс
                        progress = (i + 1) / blocks_to_record
                        add_details(f"Запись: {progress*100:.1f}% завершено")
                        
                        if i % 10 == 0:  # Обновляем статус каждые 10 блоков
                            test_window.update_idletasks()
                
                if test_data:
                    update_status("📊 Анализ записанных данных...", '#1976d2')
                    add_details("✅ Запись завершена успешно")
                    
                    # Анализ данных
                    all_audio = np.concatenate(test_data, axis=0)
                    duration = len(all_audio) / sample_rate
                    max_amplitude = np.max(np.abs(all_audio))
                    rms = np.sqrt(np.mean(all_audio**2))
                    
                    add_details("📊 Анализ данных:")
                    add_details(f"   Длительность: {duration:.2f} сек")
                    add_details(f"   Сэмплов: {len(all_audio)}")
                    add_details(f"   Макс. амплитуда: {max_amplitude:.4f}")
                    add_details(f"   RMS уровень: {rms:.4f}")
                    add_details(f"   Каналы: {all_audio.shape[1] if len(all_audio.shape) > 1 else 1}")
                    
                    # Оценка качества
                    if max_amplitude > 0.001:
                        update_status("✅ Тест завершен успешно!", '#4caf50')
                        add_details("✅ Устройство работает корректно")
                        
                        if device_type == "speaker" and max_amplitude < 0.01:
                            add_details("⚠️ Низкий уровень сигнала - возможно нет воспроизведения")
                        elif device_type == "microphone" and max_amplitude < 0.01:
                            add_details("⚠️ Низкий уровень сигнала - проверьте микрофон")
                    else:
                        update_status("⚠️ Тест завершен с предупреждениями", '#ff9800')
                        add_details("⚠️ Очень низкий уровень сигнала")
                        add_details("   Возможно устройство не получает звук")
                        
                else:
                    update_status("❌ Нет записанных данных", '#d32f2f')
                    add_details("❌ ОШИБКА: Не удалось записать данные")
                    
            except Exception as e:
                update_status("❌ Ошибка записи", '#d32f2f')
                progress_bar.stop()
                add_details(f"❌ ОШИБКА записи: {str(e)}")
                return
            
            # Тест 3: Проверка на прерывания
            update_status("🔄 Тест 3: Проверка стабильности...", '#1976d2')
            add_details("Тест 3: Проверка стабильности...")
            
            try:
                interruptions = 0
                with target_device.recorder(samplerate=sample_rate, blocksize=512) as recorder:
                    for i in range(50):  # 50 коротких записей
                        try:
                            data = recorder.record(numframes=512)
                            if data is None or len(data) == 0:
                                interruptions += 1
                        except Exception as stability_error:
                            logger.debug(f"Прерывание в тесте стабильности {i}: {stability_error}")
                            interruptions += 1
                
                if interruptions == 0:
                    add_details("✅ Стабильность отличная")
                elif interruptions < 5:
                    add_details(f"⚠️ Обнаружено {interruptions} прерываний")
                else:
                    add_details(f"❌ Много прерываний: {interruptions}")
                    
            except Exception as e:
                add_details(f"⚠️ Не удалось проверить стабильность: {str(e)}")
            
            progress_bar.stop()
            update_status("✅ Тестирование завершено!", '#4caf50')
            add_details("-" * 50)
            add_details(f"Завершено: {datetime.now().strftime('%H:%M:%S')}")
            
        except Exception as e:
            progress_bar.stop()
            update_status("❌ Критическая ошибка теста", '#d32f2f')
            add_details(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {str(e)}")
            logger.error(f"Ошибка тестирования устройства {device_name}: {e}", exc_info=True)
    
    # Запускаем тест через небольшую задержку для отображения UI
    test_window.after(100, run_test)


def refresh_audio_devices():
    """Перечитывает список аудиоустройств"""
    try:
        logger.info("Обновление списка аудиоустройств...")
        show_notification("🔄 Поиск аудиоустройств...")
        
        # Переинициализируем аудиоустройства
        success = init_audio_devices()
        
        if success:
            # Обновляем отображение в главном окне
            update_audio_device_display()
            
            # Показываем результат
            all_devices = sc.all_microphones(include_loopback=True)
            device_count = len(all_devices)
            
            show_notification(f"✅ Найдено устройств: {device_count}")
            logger.info(f"Обновление завершено, найдено {device_count} устройств")
        else:
            show_notification("❌ Ошибка обновления устройств", error=True)
            
    except Exception as e:
        logger.error(f"Ошибка обновления аудиоустройств: {e}", exc_info=True)
        show_notification("❌ Ошибка обновления устройств", error=True)


def show_settings_dialog():
    """Показывает диалог настроек"""
    global settings_window
    settings_window = tk.Toplevel(root)
    settings_window.title("⚙️ Настройки")
    settings_window.geometry("650x750")
    settings_window.resizable(False, False)
    settings_window.transient(root)
    settings_window.grab_set()

    # Применяем те же стили, что и в основном интерфейсе
    colors = {
        'primary': '#1976d2',
        'primary_dark': '#1565c0',
        'success': '#2e7d32',
        'success_dark': '#1b5e20',
        'danger': '#c62828',
        'warning': '#f57c00',
        'info': '#0277bd',
        'dark': '#263238',
        'light': '#eceff1',
        'background': '#fafafa',
        'text': '#212121',
        'text_secondary': '#757575'
    }

    settings_window.update_idletasks()
    x = root.winfo_x() + (root.winfo_width() - settings_window.winfo_width()) // 2
    y = root.winfo_y() + (root.winfo_height() - settings_window.winfo_height()) // 2
    settings_window.geometry(f"+{x}+{y}")

    # Главный контейнер с отступами
    main_container = ttk.Frame(settings_window, padding="15")
    main_container.pack(fill='both', expand=True)

    # Заголовок
    title_label = ttk.Label(main_container, 
                           text="⚙️ Настройки приложения", 
                           font=('Segoe UI', 16, 'bold'),
                           foreground=colors['primary'])
    title_label.pack(pady=(0, 15))

    notebook = ttk.Notebook(main_container)
    notebook.pack(fill='both', expand=True)

    basic_frame = ttk.Frame(notebook, padding="15")
    notebook.add(basic_frame, text="🎯 Основные")

    # Заголовок раздела аудио
    audio_header = ttk.Label(basic_frame, 
                            text="🎵 Настройки звука", 
                            font=('Segoe UI', 12, 'bold'),
                            foreground=colors['dark'])
    audio_header.pack(anchor='w', pady=(10, 10))

    # Выбор источника аудио
    record_speakers_var = tk.BooleanVar(value=AppState.record_speakers)
    record_microphone_var = tk.BooleanVar(value=AppState.record_microphone)

    ttk.Checkbutton(basic_frame, text="🔊 Записывать системный звук",
                    variable=record_speakers_var).pack(anchor='w', pady=4)
    ttk.Checkbutton(basic_frame, text="🎤 Записывать микрофон",
                    variable=record_microphone_var).pack(anchor='w', pady=4)

    # Выбор устройства для системного звука (динамиков)
    speaker_frame = ttk.LabelFrame(basic_frame, text="🔊 Системный звук", padding="12")
    speaker_frame.pack(fill='x', pady=(15, 10))
    
    # Верхняя часть с выбором устройства
    speaker_select_frame = ttk.Frame(speaker_frame)
    speaker_select_frame.pack(fill='x', pady=(0, 8))
    
    devices = get_audio_devices()

    current_speaker = AppState.selected_speaker_device if AppState.selected_speaker_device else ""
    speaker_device_var = tk.StringVar(value=current_speaker)

    speaker_device_combo = ttk.Combobox(
        speaker_select_frame,
        textvariable=speaker_device_var,
        values=devices,
        state="readonly",
        height=8,
        font=('Segoe UI', 9)
    )
    speaker_device_combo.pack(side='left', fill='x', expand=True)
    
    # Кнопки для устройства динамиков
    speaker_buttons_frame = ttk.Frame(speaker_frame)
    speaker_buttons_frame.pack(fill='x')
    
    def test_speaker_device():
        selected = speaker_device_var.get()
        if selected:
            check_audio_device_ui(selected, "speaker")
        else:
            show_notification("Сначала выберите устройство для тестирования", warning=True)
    
    ttk.Button(speaker_buttons_frame, text="🧪 Тестировать", 
               command=test_speaker_device, width=12, style='Normal.TButton').pack(side='left', padx=(0, 5))
    ttk.Button(speaker_buttons_frame, text="🔄 Обновить список", 
               command=refresh_audio_devices, width=15, style='Normal.TButton').pack(side='left')

    # Выбор устройства для микрофона
    microphone_frame = ttk.LabelFrame(basic_frame, text="🎤 Микрофон", padding="12")
    microphone_frame.pack(fill='x', pady=(15, 10))
    
    # Верхняя часть с выбором устройства
    microphone_select_frame = ttk.Frame(microphone_frame)
    microphone_select_frame.pack(fill='x', pady=(0, 8))
    
    current_microphone = AppState.selected_microphone_device if AppState.selected_microphone_device else ""
    microphone_device_var = tk.StringVar(value=current_microphone)

    microphone_device_combo = ttk.Combobox(
        microphone_select_frame,
        textvariable=microphone_device_var,
        values=devices,
        state="readonly",
        height=8,
        font=('Segoe UI', 9)
    )
    microphone_device_combo.pack(side='left', fill='x', expand=True)
    
    # Кнопки для устройства микрофона
    microphone_buttons_frame = ttk.Frame(microphone_frame)
    microphone_buttons_frame.pack(fill='x')
    
    def test_microphone_device():
        selected = microphone_device_var.get()
        if selected:
            check_audio_device_ui(selected, "microphone")
        else:
            show_notification("Сначала выберите устройство для тестирования", warning=True)
    
    ttk.Button(microphone_buttons_frame, text="🧪 Тестировать", 
               command=test_microphone_device, width=12, style='Normal.TButton').pack(side='left', padx=(0, 5))
    ttk.Button(microphone_buttons_frame, text="🔄 Обновить список", 
               command=refresh_audio_devices, width=15, style='Normal.TButton').pack(side='left')

    # Настройки папки
    folder_header = ttk.Label(basic_frame, 
                             text="📂 Папки и файлы", 
                             font=('Segoe UI', 12, 'bold'),
                             foreground=colors['dark'])
    folder_header.pack(anchor='w', pady=(20, 10))

    # Папка для сохранения видео
    folder_frame = ttk.LabelFrame(basic_frame, text="📹 Папка для видео", padding="12")
    folder_frame.pack(fill='x', pady=(0, 10))

    folder_var = tk.StringVar(value=AppState.settings.get('video_dir', Config.VIDEO_DIR))
    folder_entry = ttk.Entry(folder_frame, textvariable=folder_var, width=40, font=('Segoe UI', 9))
    folder_entry.pack(side='left', fill='x', expand=True, padx=(0, 8))

    def browse_folder():
        folder = filedialog.askdirectory(initialdir=folder_var.get())
        if folder:
            folder_var.set(folder)

    ttk.Button(folder_frame, text="📁 Обзор", command=browse_folder, 
               width=10, style='Normal.TButton').pack(side='right')

    # Настройки приложения
    app_header = ttk.Label(basic_frame, 
                          text="⚙️ Поведение приложения", 
                          font=('Segoe UI', 12, 'bold'),
                          foreground=colors['dark'])
    app_header.pack(anchor='w', pady=(20, 10))

    # Чекбоксы
    options_frame = ttk.LabelFrame(basic_frame, text="🔧 Дополнительные настройки", padding="12")
    options_frame.pack(fill='x', pady=(0, 10))

    auto_clear_var = tk.BooleanVar(value=AppState.settings.get('auto_clear_temp', True))
    ttk.Checkbutton(options_frame, text="🗑️ Автоматически очищать временные файлы",
                    variable=auto_clear_var).pack(anchor='w', pady=4)

    auto_open_var = tk.BooleanVar(value=AppState.settings.get('auto_open_folder', False))
    ttk.Checkbutton(options_frame, text="📂 Открывать папку после записи",
                    variable=auto_open_var).pack(anchor='w', pady=4)

    notifications_var = tk.BooleanVar(value=AppState.settings.get('show_notifications', True))
    ttk.Checkbutton(options_frame, text="📢 Показывать уведомления",
                    variable=notifications_var).pack(anchor='w', pady=4)

    hotkeys_var = tk.BooleanVar(value=AppState.settings.get('enable_hotkeys', True))
    ttk.Checkbutton(options_frame, text="⌨️ Включить горячие клавиши",
                    variable=hotkeys_var).pack(anchor='w', pady=4)

    # Кнопки сохранения/отмены
    button_frame = ttk.Frame(main_container)
    button_frame.pack(fill='x', pady=(15, 0))

    def save_settings_callback():
        AppState.settings.update({
            'video_dir': folder_var.get(),
            'auto_clear_temp': auto_clear_var.get(),
            'auto_open_folder': auto_open_var.get(),
            'show_notifications': notifications_var.get(),
            'enable_hotkeys': hotkeys_var.get()
        })

        # Сохраняем выбранные устройства и настройки записи
        AppState.record_speakers = record_speakers_var.get()
        AppState.record_microphone = record_microphone_var.get()

        # Сохраняем выбранные устройства
        if speaker_device_var.get():
            select_audio_device('speaker', speaker_device_var.get())

        if microphone_device_var.get():
            select_audio_device('microphone', microphone_device_var.get())

        if save_settings():
            show_notification("Настройки сохранены ✅")
            settings_window.destroy()

            # Применяем изменения
            if hotkeys_var.get():
                setup_global_hotkeys()
            else:
                try:
                    keyboard.unhook_all()
                except:
                    pass

            update_video_list()
        else:
            show_notification("Ошибка сохранения настроек", error=True)

    ttk.Button(button_frame, text="💾 Сохранить", command=save_settings_callback,
               style="Accent.TButton", width=15).pack(side='right', padx=4)
    ttk.Button(button_frame, text="❌ Отмена", command=settings_window.destroy, 
               style='Normal.TButton', width=15).pack(side='right', padx=4)


def safe_update_main_status(text, color="#388e3c"):
    """Безопасно обновляет статус в основном окне"""

    def update():
        try:
            if 'status_label' in globals():
                # Дополнительная проверка через try/except
                status_label.config(text=text, foreground=color)
        except Exception as e:
            logger.debug(f"Не удалось обновить статус: {e}")

    if 'root' in globals():
        root.after(0, update)


def update_timer():
    """Обновляет таймер и статистику"""
    if AppState.recording and not AppState.paused:
        elapsed = int(time.time() - AppState.recording_start_time)
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)

        if hours > 0:
            timer_text = f"● Запись: {hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            timer_text = f"● Запись: {minutes:02d}:{seconds:02d}"

        status_label.config(text=timer_text, foreground="#d32f2f")

    elif AppState.recording and AppState.paused:
        status_label.config(text="⏸️ Пауза", foreground="#f57c00")

    # ВОССТАНАВЛИВАЕМ ОБНОВЛЕНИЕ СТАТИСТИКИ
    get_system_performance()
    if 'performance_label' in globals() and performance_label.winfo_exists():
        stats = AppState.performance_stats
        performance_label.config(
            text=f"⚙️ CPU: {stats['cpu_usage']:.0f}% | 🎛️ RAM: {stats['memory_usage']:.0f}% | 💾 Диск: {stats['disk_usage']:.0f}%",
            foreground="#5d4037"
        )

    root.after(1000, update_timer)


def setup_global_hotkeys():
    """Настраивает глобальные горячие клавиши"""
    try:
        keyboard.unhook_all()

        if AppState.settings.get('enable_hotkeys', True):
            keyboard.add_hotkey('F9', lambda: root.after(0, start_recording))
            keyboard.add_hotkey('F10', lambda: root.after(0, stop_recording))
            keyboard.add_hotkey('F11', lambda: root.after(0, toggle_pause))
            keyboard.add_hotkey('alt+print screen', lambda: root.after(0, take_screenshot))
            logger.info("Горячие клавиши настроены")
            show_notification("Горячие клавиши активированы ⌨️")
    except Exception as e:
        logger.error(f"Ошибка настройки горячих клавиш: {e}", exc_info=True)
        show_notification("Ошибка настройки горячих клавиш", error=True)


# ================================
# GUI И ИНТЕРФЕЙС
# ================================

def update_video_list():
    """Обновляет список записанных видео"""
    if 'video_listbox' not in globals():
        return

    video_listbox.delete(0, END)

    try:
        video_dir = AppState.settings.get('video_dir', Config.VIDEO_DIR)
        if not os.path.exists(video_dir):
            os.makedirs(video_dir, exist_ok=True)

        files = []
        for f in os.listdir(video_dir):
            if f.lower().endswith(('.mp4', '.mkv', '.webm', '.avi')):
                files.append(os.path.join(video_dir, f))

        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        # Без лимита - показываем все файлы
        for filepath in files:
            mod_time = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d %H:%M")
            file_size = os.path.getsize(filepath) / (1024 * 1024)
            filename = os.path.basename(filepath)

            duration = get_video_duration(filepath)
            duration_text = f", {duration}" if duration else ""

            display_text = f"{mod_time} — {filename} ({file_size:.1f} MB{duration_text})"
            video_listbox.insert(END, display_text)

        if files:
            video_count_label.config(text=f"📁 Файлов: {len(files)}")
            logger.debug(f"Список видео обновлен: {len(files)} файлов")
        else:
            video_count_label.config(text="📁 Файлов нет")
            video_listbox.insert(END, "Записей еще нет")
            logger.debug("Список видео обновлен: файлов нет")

    except Exception as e:
        logger.error(f"Ошибка обновления списка видео: {e}", exc_info=True)
        video_listbox.insert(END, "Ошибка загрузки списка")


def create_gui():
    """Создает графический интерфейс"""
    global root, start_button, stop_button, pause_button, status_label
    global video_listbox, notification_listbox, audio_status_label
    global audio_indicator, video_count_label, performance_label
    global monitor_descriptions

    load_settings()

    if not monitor_descriptions:
        monitor_descriptions = get_monitor_descriptions()

    root = tk.Tk()
    root.title(f"{Config.APP_NAME} v{Config.VERSION}")
    root.geometry("1000x750")
    root.minsize(900, 650)
    root.protocol("WM_DELETE_WINDOW", on_closing)

    try:
        icon_path = os.path.join(Config.APPLICATION_PATH, "icon.ico")
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
            logger.info(f"Иконка установлена: {icon_path}")
    except Exception as e:
        logger.warning(f"Не удалось установить иконку: {e}")

    style = ttk.Style()
    style.theme_use('clam')

    colors = {
        'primary': '#1976d2',
        'primary_dark': '#1565c0',
        'primary_light': '#42a5f5',
        'secondary': '#ff6f00',
        'secondary_dark': '#e65100',
        'secondary_light': '#ffa726',
        'success': '#2e7d32',
        'success_dark': '#1b5e20',
        'success_light': '#66bb6a',
        'danger': '#c62828',
        'danger_dark': '#b71c1c',
        'danger_light': '#ef5350',
        'warning': '#f57c00',
        'warning_dark': '#e65100',
        'warning_light': '#ffa726',
        'info': '#0277bd',
        'info_dark': '#01579b',
        'info_light': '#039be5',
        'dark': '#263238',
        'light': '#eceff1',
        'background': '#fafafa',
        'surface': '#ffffff',
        'text': '#212121',
        'text_secondary': '#757575',
        'border': '#e0e0e0',
        'shadow': '#000000'
    }

    # Настройка стилей для кнопок с улучшенным дизайном
    style.configure('Title.TLabel', font=('Segoe UI', 16, 'bold'), foreground=colors['primary'])
    style.configure('Subtitle.TLabel', font=('Segoe UI', 12, 'bold'), foreground=colors['dark'])
    style.configure('Status.TLabel', font=('Segoe UI', 10), foreground=colors['text'])
    
    # Основные стили кнопок
    style.configure('Normal.TButton', 
                   font=('Segoe UI', 10, 'bold'),
                   padding=(12, 8),
                   relief='flat',
                   background=colors['primary'],
                   foreground='white')
    style.map('Normal.TButton',
              background=[('active', colors['primary_dark']), 
                         ('pressed', colors['primary_dark'])],
              relief=[('pressed', 'flat'), ('active', 'flat')])
    
    # Акцентная кнопка (главная)
    style.configure('Accent.TButton', 
                   font=('Segoe UI', 11, 'bold'),
                   padding=(15, 10),
                   relief='flat',
                   background=colors['success'],
                   foreground='white')
    style.map('Accent.TButton',
              background=[('active', colors['success_dark']), 
                         ('pressed', colors['success_dark'])],
              relief=[('pressed', 'flat'), ('active', 'flat')])
    
    # Кнопка записи (красная)
    style.configure('Recording.TButton', 
                   font=('Segoe UI', 11, 'bold'),
                   padding=(15, 10),
                   relief='flat',
                   background=colors['danger'],
                   foreground='white')
    style.map('Recording.TButton',
              background=[('active', colors['danger_dark']), 
                         ('pressed', colors['danger_dark'])],
              relief=[('pressed', 'flat'), ('active', 'flat')])
    
    # Отключенная кнопка
    style.configure('Disabled.TButton', 
                   font=('Segoe UI', 10),
                   padding=(12, 8),
                   relief='flat',
                   background=colors['border'],
                   foreground=colors['text_secondary'])
    
    # Маленькая кнопка для инструментов
    style.configure('Tool.TButton',
                   font=('Segoe UI', 9),
                   padding=(6, 4),
                   relief='flat',
                   background=colors['info'],
                   foreground='white')
    style.map('Tool.TButton',
              background=[('active', colors['info_dark']), 
                         ('pressed', colors['info_dark'])],
              relief=[('pressed', 'flat'), ('active', 'flat')])
    
    # Стили для фреймов и меток
    style.configure('TFrame', background=colors['background'])
    style.configure('TLabel', background=colors['background'], font=('Segoe UI', 9))
    style.configure('TLabelframe', 
                   background=colors['background'], 
                   font=('Segoe UI', 11, 'bold'),
                   foreground=colors['primary'],
                   relief='solid',
                   borderwidth=1)
    style.configure('TLabelframe.Label', 
                   background=colors['background'], 
                   font=('Segoe UI', 11, 'bold'),
                   foreground=colors['primary'])

    main_container = ttk.Frame(root, padding="10")
    main_container.pack(fill=tk.BOTH, expand=True)

    header_frame = ttk.Frame(main_container)
    header_frame.pack(fill=tk.X, pady=(0, 10))





    # ВОССТАНАВЛИВАЕМ ПАНЕЛЬ СТАТУСА СИСТЕМЫ
    stats_frame = ttk.Frame(header_frame)
    stats_frame.pack(side=tk.RIGHT)

    performance_label = ttk.Label(stats_frame, text="⚙️ CPU: 0% | 🎛️ RAM: 0% | 💾 Диск: 0%",
                                  style='Status.TLabel')
    performance_label.pack()


    title_label = ttk.Label(header_frame,
                            text=f"🎬 {Config.APP_NAME} v{Config.VERSION}",
                            style='Title.TLabel')
    title_label.pack(side=tk.LEFT)

    settings_frame = ttk.LabelFrame(main_container, text="🎥 Управление записью", padding="12")
    settings_frame.pack(fill=tk.X, pady=(0, 12))

    top_row = ttk.Frame(settings_frame)
    top_row.pack(fill=tk.X, pady=(0, 8))

    monitor_frame = ttk.Frame(top_row)
    monitor_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

    ttk.Label(monitor_frame, text="🖥️ Записывать экран:",
              style='Subtitle.TLabel').pack(anchor='w')

    monitor_var = tk.StringVar()
    monitor_combo = ttk.Combobox(
        monitor_frame,
        textvariable=monitor_var,
        values=monitor_descriptions,
        state="readonly",
        height=4,
        font=('Segoe UI', 9)
    )
    monitor_combo.pack(fill=tk.X, pady=(3, 0))

    if monitor_descriptions:
        monitor_var.set(monitor_descriptions[0])
        AppState.selected_monitor_index = 1

    def on_monitor_change(*args):
        try:
            index = monitor_descriptions.index(monitor_var.get())
            AppState.selected_monitor_index = index + 1
            logger.info(f"Выбран монитор: {monitor_var.get()}")
            show_notification(f"Выбран монитор: {monitor_var.get()}")
        except ValueError:
            logger.warning(f"Выбран неизвестный монитор: {monitor_var.get()}")

    monitor_var.trace("w", on_monitor_change)

    audio_frame = ttk.Frame(top_row)
    audio_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

    ttk.Label(audio_frame, text="🎵 Звук для записи:",
              style='Subtitle.TLabel').pack(anchor='w')

    audio_status_frame = ttk.Frame(audio_frame)
    audio_status_frame.pack(fill=tk.X, pady=(3, 0))

    audio_status_label = ttk.Label(audio_status_frame,
                                   text="Загрузка устройств...",
                                   wraplength=350,
                                   foreground=colors['primary'])
    audio_status_label.pack(side=tk.LEFT)

    audio_indicator = ttk.Label(audio_status_frame, text="⚫", font=('Segoe UI', 14))
    audio_indicator.pack(side=tk.LEFT, padx=5)
    
    # Кнопка обновления аудиоустройств
    audio_refresh_btn = ttk.Button(audio_status_frame, text="🔄", 
                                  command=refresh_audio_devices, 
                                  width=3, style='Tool.TButton')
    audio_refresh_btn.pack(side=tk.RIGHT, padx=(5, 0))

    # Обновляем отображение аудиоустройств

    control_buttons = ttk.Frame(settings_frame)
    control_buttons.pack(fill=tk.X, pady=(12, 0))

    start_button = ttk.Button(control_buttons,
                              text="🎬 Начать запись [F9]",
                              command=start_recording,
                              style='Accent.TButton',
                              width=28)
    start_button.pack(side=tk.LEFT, padx=4)

    stop_button = ttk.Button(control_buttons,
                             text="⏹️ Остановить [F10]",
                             command=stop_recording,
                             style='Recording.TButton',
                             width=28,
                             state="disabled")
    stop_button.pack(side=tk.LEFT, padx=4)

    pause_button = ttk.Button(control_buttons,
                              text="⏸️ Пауза [F11]",
                              command=toggle_pause,
                              style='Normal.TButton',
                              width=28,
                              state="disabled")
    pause_button.pack(side=tk.LEFT, padx=4)

    status_frame = ttk.Frame(settings_frame)
    status_frame.pack(fill=tk.X, pady=(8, 0))

    status_label = ttk.Label(status_frame,
                             text="✅ Готов к записи",
                             font=('Segoe UI', 11, 'bold'),
                             foreground=colors['success'])
    status_label.pack(side=tk.LEFT)

    ttk.Label(status_frame,
              text="🎯 Горячие клавиши: F9 - Старт | F10 - Стоп | F11 - Пауза | Alt+PrtScr - Скриншот",
              foreground=colors['secondary'],
              font=('Segoe UI', 9)).pack(side=tk.RIGHT)

    content_frame = ttk.Frame(main_container)
    content_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

    video_frame = ttk.LabelFrame(content_frame, text="📹 Записанные видео", padding="10")
    video_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 12))

    list_header = ttk.Frame(video_frame)
    list_header.pack(fill=tk.X, pady=(0, 8))

    ttk.Label(list_header, text="Ваши записи:",
              style='Subtitle.TLabel').pack(side=tk.LEFT)

    video_count_label = ttk.Label(list_header, text="📁 Файлов: 0",
                                  foreground=colors['secondary'])
    video_count_label.pack(side=tk.RIGHT)

    list_controls = ttk.Frame(video_frame)
    list_controls.pack(fill=tk.X, pady=(0, 8))

    ttk.Button(list_controls, text="📸 Скриншот [Alt+PrtSc]",
               command=take_screenshot, width=28, style='Normal.TButton').pack(side=tk.LEFT, padx=3)

    ttk.Button(list_controls, text="🎬 Открыть последнее",
               command=open_last_recording, width=28, style='Normal.TButton').pack(side=tk.LEFT, padx=3)
    ttk.Button(list_controls, text="🔄 Обновить список",
               command=update_video_list, width=28, style='Normal.TButton').pack(side=tk.LEFT, padx=3)

    list_container = ttk.Frame(video_frame)
    list_container.pack(fill=tk.BOTH, expand=True)

    video_scrollbar = ttk.Scrollbar(list_container)
    video_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    video_listbox = Listbox(
        list_container,
        width=60,
        height=12,
        font=('Consolas', 9),
        bg=colors['light'],
        selectbackground=colors['primary'],
        selectforeground='white',
        yscrollcommand=video_scrollbar.set
    )
    video_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    video_scrollbar.config(command=video_listbox.yview)

    video_listbox.bind("<Double-1>", on_video_double_click)
    video_listbox.bind("<Button-3>", on_video_right_click)

    def on_video_key_press(event):
        if event.keysym == 'Delete':
            delete_selected_file()
        elif event.keysym == 'Return':
            on_video_double_click(event)
        elif event.keysym == 'c' and event.state & 0x4:
            copy_filepath_to_clipboard()
        elif event.keysym == 'F2':
            rename_selected_file()
        elif event.keysym == 'F5':
            update_video_list()

    video_listbox.bind("<Key>", on_video_key_press)

    tools_frame = ttk.LabelFrame(content_frame, text="🛠️ Инструменты", padding="10")
    tools_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(12, 0))

    tools_buttons = ttk.Frame(tools_frame)
    tools_buttons.pack(fill=tk.X, pady=(0, 10))

    ttk.Button(tools_buttons, text="⚙️ Настройки",
               command=show_settings_dialog, width=28, style='Normal.TButton').pack(pady=4)

    ttk.Button(tools_buttons, text="📂 Скриншоты",
               command=lambda: open_folder(Config.SNAPSHOTS_DIR), width=28, style='Normal.TButton').pack(pady=4)

    ttk.Button(tools_buttons, text="📂 Видео",
               command=lambda: open_folder(), width=28, style='Normal.TButton').pack(pady=4)

    ttk.Button(tools_buttons, text="🎵 Аудио диагностика",
               command=debug_audio_devices, width=28, style='Normal.TButton').pack(pady=4)

    notify_frame = ttk.LabelFrame(main_container, text="📢 Системные уведомления", padding="10")
    notify_frame.pack(fill=tk.X, pady=(0, 12))

    notification_listbox = Listbox(
        notify_frame,
        height=3,
        font=('Segoe UI', 9),
        bg=colors['light'],
        relief='flat'
    )
    notification_listbox.pack(fill=tk.X)

    def on_notification_right_click(event):
        context_menu = tk.Menu(root, tearoff=0)
        context_menu.add_command(label="🗑️ Очистить все уведомления",
                                 command=lambda: notification_listbox.delete(0, tk.END))
        context_menu.tk_popup(event.x_root, event.y_root)

    notification_listbox.bind("<Button-3>", on_notification_right_click)

    for notification in AppState.pending_notifications:
        show_notification(notification)
    AppState.pending_notifications.clear()

    footer_frame = ttk.Frame(main_container)
    footer_frame.pack(fill=tk.X, pady=(10, 0))

    ttk.Label(footer_frame,
              text=f"© {Config.AUTHOR} | {Config.APP_NAME} v{Config.VERSION} | 🚀 Профессиональная запись экрана",
              foreground=colors['secondary'],
              font=('Segoe UI', 9)).pack(side=tk.LEFT)

    recording_indicator = tk.Label(footer_frame, text="●", font=('Segoe UI', 12), foreground=colors['light'])
    recording_indicator.pack(side=tk.RIGHT)

    def blink_recording_indicator():
        if AppState.recording and not AppState.paused:
            current_color = recording_indicator.cget("foreground")
            new_color = colors['danger'] if current_color == colors['light'] else colors['light']
            recording_indicator.config(foreground=new_color)
        elif AppState.recording and AppState.paused:
            recording_indicator.config(foreground=colors['warning'])
        else:
            recording_indicator.config(foreground=colors['light'])

        root.after(500, blink_recording_indicator)

    blink_recording_indicator()

    def focus_video_list(event=None):
        video_listbox.focus_set()

    root.bind("<Control-l>", focus_video_list)

    logger.info("GUI создан успешно")
    return root


def on_closing():
    """Обработчик закрытия окна"""
    logger.info("Закрытие приложения...")

    if AppState.recording:
        response = messagebox.askyesnocancel(
            "Выход",
            "Запись ещё идёт. Выберите действие:\n\n"
            "✅ Да - остановить и выйти\n"
            "❌ Нет - выйти без остановки (может повредить файлы)\n"
            "⚠️ Отмена - остаться в программе"
        )

        if response is None:
            logger.info("Закрытие отменено пользователем")
            return
        elif response:
            logger.info("Остановка записи перед закрытием...")
            stop_recording()
            time.sleep(1)
        else:
            logger.warning("Выход без остановки записи")

    try:
        keyboard.unhook_all()
        logger.info("Горячие клавиши отключены")
    except:
        logger.warning("Не удалось отключить горячие клавиши")

    save_settings()

    logger.info("Приложение завершено")
    root.destroy()


def on_video_double_click(event):
    """Обработчик двойного клика по видео в списке - открывает файл"""
    selection = video_listbox.curselection()
    if not selection:
        show_notification("Выберите файл из списка ⚠️", warning=True)
        return

    line = video_listbox.get(selection[0])

    try:
        if " — " in line:
            filename = line.split(" — ", 1)[1].split(" (", 1)[0].strip()
        else:
            filename = line.strip()

        video_dir = AppState.settings.get('video_dir', Config.VIDEO_DIR)
        filepath = os.path.join(video_dir, filename)

        if os.path.exists(filepath):
            if safe_open_file(filepath):
                AppState.last_recording_file = filepath
                show_notification(f"Открыт файл: {filename} 🎬")
        else:
            logger.warning(f"Файл не найден: {filepath}")
            show_notification(f"Файл не найден: {filename} ⚠️", warning=True)

    except Exception as e:
        logger.error(f"Ошибка обработки двойного клика: {e}", exc_info=True)
        show_notification("Ошибка обработки файла", error=True)


def on_video_right_click(event):
    """Обработчик правого клика по видео в списке - показывает контекстное меню"""
    try:
        index = video_listbox.nearest(event.y)
        video_listbox.selection_clear(0, tk.END)
        video_listbox.selection_set(index)
        video_listbox.activate(index)

        context_menu = tk.Menu(root, tearoff=0)
        context_menu.add_command(label="Открыть файл", command=on_video_double_click)
        context_menu.add_command(label="Копировать путь", command=copy_filepath_to_clipboard)
        context_menu.add_command(label="Переименовать", command=rename_selected_file)
        context_menu.add_separator()
        context_menu.add_command(label="Удалить файл", command=delete_selected_file)
        context_menu.add_separator()
        context_menu.add_command(label="Показать в папке", command=show_file_in_folder)

        context_menu.tk_popup(event.x_root, event.y_root)

    except Exception as e:
        logger.error(f"Ошибка создания контекстного меню: {e}", exc_info=True)


def delete_selected_file():
    """Удаляет выбранный файл"""
    selection = video_listbox.curselection()
    if not selection:
        show_notification("Выберите файл для удаления ⚠️", warning=True)
        return

    line = video_listbox.get(selection[0])

    try:
        if " — " in line:
            filename = line.split(" — ", 1)[1].split(" (", 1)[0].strip()
        else:
            filename = line.strip()

        video_dir = AppState.settings.get('video_dir', Config.VIDEO_DIR)
        filepath = os.path.join(video_dir, filename)

        if os.path.exists(filepath):
            confirm = messagebox.askyesno(
                "Подтверждение удаления",
                f"Вы уверены, что хотите удалить файл?\n\n{filename}\n\n"
                f"Размер: {os.path.getsize(filepath) / (1024 * 1024):.1f} МБ"
            )

            if confirm:
                os.remove(filepath)
                logger.info(f"Файл удален: {filepath}")
                show_notification(f"Файл удален: {filename} 🗑️")

                if AppState.last_recording_file == filepath:
                    AppState.last_recording_file = None

                update_video_list()
        else:
            logger.warning(f"Файл не найден: {filepath}")
            show_notification("Файл не найден ⚠️", warning=True)

    except Exception as e:
        logger.error(f"Ошибка удаления файла: {e}", exc_info=True)
        show_notification("Ошибка удаления файла", error=True)


def show_file_in_folder():
    """Показывает файл в проводнике"""
    selection = video_listbox.curselection()
    if not selection:
        show_notification("Выберите файл из списка ⚠️", warning=True)
        return

    line = video_listbox.get(selection[0])

    try:
        if " — " in line:
            filename = line.split(" — ", 1)[1].split(" (", 1)[0].strip()
        else:
            filename = line.strip()

        video_dir = AppState.settings.get('video_dir', Config.VIDEO_DIR)
        filepath = os.path.join(video_dir, filename)

        if os.path.exists(filepath):
            try:
                logger.info(f"Показ файла в папке: {filepath}")
                if sys.platform == "win32":
                    subprocess.run(f'explorer /select,"{filepath}"', shell=True)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", filepath])
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(filepath)])

                show_notification(f"Показан файл в папке: {filename} 📂")

            except Exception as e:
                logger.error(f"Ошибка показа файла в папке: {e}", exc_info=True)
                show_notification("Не удалось показать файл в папке", error=True)
        else:
            logger.warning(f"Файл не найден: {filepath}")
            show_notification("Файл не найден ⚠️", warning=True)

    except Exception as e:
        logger.error(f"Ошибка показа файла в папке: {e}", exc_info=True)
        show_notification("Ошибка обработки файла", error=True)


# ================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ================================

if __name__ == "__main__":
    print(f"\n{'=' * 60}")
    print(f"🚀 Запуск {Config.APP_NAME} v{Config.VERSION}")
    print(f"{'=' * 60}\n")

    try:
        ensure_directories()
        monitor_descriptions = get_monitor_descriptions()

        print("🖥️ Поиск мониторов...")
        for i, desc in enumerate(monitor_descriptions, 1):
            print(f"  {i}. {desc}")

        print("\n🎵 Инициализация аудио...")
        init_audio_devices()
        update_audio_device_display()

        print("\n🎨 Создание интерфейса...")
        root = create_gui()

        print("\n⌨️ Настройка горячих клавиш...")
        setup_global_hotkeys()

        print("\n⏰ Запуск таймеров...")
        update_timer()
        update_video_list()

        print(f"\n✅ {Config.APP_NAME} запущен и готов к работе!")
        print(f"📁 Папка для видео: {AppState.settings.get('video_dir', Config.VIDEO_DIR)}")
        print(f"🗑️ Папка временных файлов: {Config.TEMP_DIR}")
        print(f"📸 Папка скриншотов: {Config.SNAPSHOTS_DIR}")
        print(f"🎯 Качество: 1080p @ {Config.DEFAULT_FPS} FPS")
        print(f"💾 Формат: {Config.DEFAULT_FORMAT} (H.264/AAC)")
        print(f"🎵 Записывать системный звук: {AppState.record_speakers}")
        print(f"🎤 Записывать микрофон: {AppState.record_microphone}")

        if AppState.record_speakers and AppState.selected_speaker_device:
            print(f"🔊 Устройство динамиков: {AppState.selected_speaker_device[:50]}")

        if AppState.record_microphone and AppState.selected_microphone_device:
            print(f"🎤 Устройство микрофона: {AppState.selected_microphone_device[:50]}")

        print("\n📋 Логи записываются в: screen_recorder.log")
        print(f"{'=' * 60}\n")

        show_notification(f"{Config.APP_NAME} v{Config.VERSION} запущен и готов к записи! 🚀")

        root.mainloop()

    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске: {e}", exc_info=True)
        print(f"\n❌ Критическая ошибка: {e}")
        traceback.print_exc()

        try:
            with open('screen_recorder_crash.log', 'w', encoding='utf-8') as f:
                f.write(f"Критическая ошибка при запуске: {datetime.now()}\n")
                f.write(f"Ошибка: {e}\n")
                f.write("\nТрассировка:\n")
                traceback.print_exc(file=f)
            print("Детали ошибки записаны в screen_recorder_crash.log")
        except:
            pass

        input("\nНажмите Enter для выхода...")