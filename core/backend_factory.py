from core.wsl_manager import WSLManager


class BackendFactory:
    @staticmethod
    def create(config):
        return WSLManager(config=config)
