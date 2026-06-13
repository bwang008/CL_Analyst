from src.live_execution.interfaces.data_feed_interface import DataFeedClient
from src.live_execution.interfaces.execution_interface import ExecutionClient

class DataFeedFactory:
    @staticmethod
    def create(source: str, **kwargs) -> DataFeedClient:
        if source.lower() == 'ibkr':
            from src.live_execution.adapters.ibkr_data_feed import IBKRDataFeedClient
            return IBKRDataFeedClient(**kwargs)
        raise ValueError(f"Unknown data source: {source}")

class ExecutionFactory:
    @staticmethod
    def create(source: str, **kwargs) -> ExecutionClient:
        if source.lower() == 'ibkr':
            from src.live_execution.adapters.ibkr_execution import IBKRExecutionClient
            return IBKRExecutionClient(**kwargs)
        raise ValueError(f"Unknown exec source: {source}")
