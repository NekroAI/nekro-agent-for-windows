from core.wsl import WSLManager


class BackendFactory:
    @staticmethod
    def create(config):
        return WSLManager(config=config)
