import logging
import os
import sys
import yaml # Requires PyYAML library (pip install PyYAML)
from typing import Any

logger = logging.getLogger(__name__)

class Config:
    config_filename = "config.yaml"
    
    @staticmethod
    def get_config() -> dict[str, Any] | None:
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        config_path = os.path.join(project_root_dir, "config", Config.config_filename)
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)
                return config_data
        except FileNotFoundError:
            logger.critical(f"Configuration file '{Config.config_filename}' not found.")
            sys.exit(1)
            return None
        except yaml.YAMLError as e:
            logger.critical(f"Error parsing YAML file '{Config.config_filename}': {e}")
            sys.exit(1)
            return None
        except ImportError:
            logger.critical("PyYAML library not found for loading. Please install it: pip install PyYAML")
            sys.exit(1)
            return None
        except Exception as e:
            logger.critical(f"An error occurred while loading the config: {e}")
            sys.exit(1)
            return None
        
    @staticmethod
    def get_server_config() -> dict[str, Any]:
        config_data = Config.get_config()
        if not config_data:
            logger.error("Error: Cannot search in empty or invalid configuration.")
            return {}
        
        return config_data.get('server', {})
    
    @staticmethod
    def get_transcription_config() -> dict[str, Any]:
        config_data = Config.get_config()
        if not config_data:
            logger.error("Error: Cannot search in empty or invalid configuration.")
            return {}
        
        return config_data.get('transcription', {})
        
    @staticmethod
    def get_all_streamers_config() -> list[dict[str, Any]]:
        config_data = Config.get_config()
        if not config_data:
            logger.error("Error: Cannot search in empty or invalid configuration.")
            return []
        
        return config_data.get('streamers', [])
        
    @staticmethod
    def get_streamer_config(key: str) -> dict[str, Any]:
        config_data = Config.get_config()
        if not config_data:
            logger.error("Error: Cannot search in empty or invalid configuration.")
            return {}
        
        streamer_list = config_data.get('streamers', [])
        
        if not isinstance(streamer_list, list):
            logger.warning("'streamers' key in config is not a list.")
            return {}
        
        for streamer in streamer_list:
            if isinstance(streamer, dict) and 'key' in streamer:
                if streamer['key'] == key:
                    return streamer

        return {}
