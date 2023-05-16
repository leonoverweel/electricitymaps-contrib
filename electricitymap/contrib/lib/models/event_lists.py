from abc import ABC, abstractmethod
from datetime import datetime
from logging import Logger
from typing import Any, Dict, List, Optional, Tuple, Iterator

import numpy as np
import pandas as pd

from electricitymap.contrib.lib.models.events import (
    Event,
    EventSourceType,
    Exchange,
    Price,
    ProductionBreakdown,
    ProductionMix,
    StorageMix,
    TotalConsumption,
    TotalProduction,
)
from electricitymap.contrib.lib.types import ZoneKey


class EventList(ABC):
    """A wrapper around Events lists."""

    logger: Logger
    events: List[Event]

    def __init__(self, logger: Logger):
        self.events = list()
        self.logger = logger

    @abstractmethod
    def append(self, **kwargs):
        """Handles creation of events and adding it to the batch."""
        # TODO Handle one day the creation of mixed batches.
        pass

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def filter_events(self, start: Optional[datetime], end: Optional[datetime]) -> None:
        """Filter events to keep only those between start and end."""
        if start is None and end is None:
            return
        if start is None:
            self.events = list(filter(lambda x: x.datetime <= end, self.events))
            return
        if end is None:
            self.events = list(filter(lambda x: start <= x.datetime, self.events))
            return
        self.events = list(filter(lambda x: start <= x.datetime <= end, self.events))

    def to_list(self) -> List[Dict[str, Any]]:
        return [event.to_dict() for event in self.events]


class MergeableList(EventList, ABC):
    """A wrapper around Events lists that can be merged together."""

    @classmethod
    def is_completly_empty(
        cls, ungrouped_events: List["MergeableList"], logger: Logger
    ) -> "MergeableList":
        """Merge multiple lists of events into one."""
        if len(ungrouped_events) == 0:
            return True
        if all(len(exchanges.events) == 0 for exchanges in ungrouped_events):
            logger.warning(f"All {cls.__name__} are empty.")
            return True
        return False

    @classmethod
    def get_unique_zone_source(
        cls,
        events: pd.DataFrame,
    ) -> Tuple[ZoneKey, str, EventSourceType]:
        """
        Given a concatenated dataframe of events, return the unique zone, source and source type.
        Raises an error if there are multiple zones or source types.
        It assumes that zoneKey, source and sourceType are present in the dataframe's columns.
        """
        sources = events["source"].unique()
        sources = ", ".join(sources)
        zones = events["zoneKey"].unique()
        if len(zones) != 1:
            raise ValueError(
                f"Trying to merge {cls.__name__} from multiple zones \
                , got {len(zones)}: {', '.join(zones)}"
            )
        source_types = events["sourceType"].unique()
        if len(source_types) != 1:
            raise ValueError(
                f"Trying to merge {cls.__name__} from multiple source types \
                , got {len(source_types)}: {', '.join(source_types)}"
            )
        return zones[0], sources, source_types[0]


class ExchangeList(MergeableList):
    events: List[Exchange]

    def append(
        self,
        zoneKey: ZoneKey,
        datetime: datetime,
        source: str,
        netFlow: float,
        sourceType: EventSourceType = EventSourceType.measured,
    ):
        event = Exchange.create(
            self.logger, zoneKey, datetime, source, netFlow, sourceType
        )
        if event:
            self.events.append(event)

    @staticmethod
    def merge_exchanges(
        ungrouped_exchanges: List["ExchangeList"], logger: Logger
    ) -> "ExchangeList":
        """
        Given multiple parser outputs, sum the netflows of corresponding datetimes
        to create a unique exchange list. Sources will be aggregated in a
        comma-separated string. Ex: "entsoe, eia".
        """
        exchanges = ExchangeList(logger)
        if ExchangeList.is_completly_empty(ungrouped_exchanges, logger):
            return exchanges

        # Create a dataframe for each parser output, then flatten the exchanges.
        exchange_dfs = [
            pd.json_normalize(exchanges.to_list()).set_index("datetime")
            for exchanges in ungrouped_exchanges
            if len(exchanges.events) > 0
        ]

        exchange_df = pd.concat(exchange_dfs)
        exchange_df = exchange_df.rename(columns={"sortedZoneKeys": "zoneKey"})
        zone_key, sources, source_type = ExchangeList.get_unique_zone_source(
            exchange_df
        )
        exchange_df = exchange_df.groupby(level=0, dropna=False).sum()
        for datetime, row in exchange_df.iterrows():
            exchanges.append(zone_key, datetime, sources, row["netFlow"], source_type)

        return exchanges


class ProductionBreakdownList(MergeableList):
    events: List[ProductionBreakdown]

    def append(
        self,
        zoneKey: ZoneKey,
        datetime: datetime,
        source: str,
        production: Optional[ProductionMix] = None,
        storage: Optional[StorageMix] = None,
        sourceType: EventSourceType = EventSourceType.measured,
    ):
        event = ProductionBreakdown.create(
            self.logger, zoneKey, datetime, source, production, storage, sourceType
        )
        if event:
            self.events.append(event)

    @staticmethod
    def merge_production_breakdowns(
        ungrouped_production_breakdowns: List["ProductionBreakdownList"],
        logger: Logger,
    ) -> "ProductionBreakdownList":
        """
        Given multiple parser outputs, sum the production and storage
        of corresponding datetimes to create a unique production breakdown list.
        Sources will be aggregated in a comma-separated string. Ex: "entsoe, eia".
        There should be only one zone in the list of production breakdowns.
        """
        production_breakdowns = ProductionBreakdownList(logger)
        if ProductionBreakdownList.is_completly_empty(
            ungrouped_production_breakdowns, logger
        ):
            return production_breakdowns

        # Create a dataframe for each parser output, then flatten the power mixes.
        prod_and_storage_dfs = [
            pd.json_normalize(breakdowns.to_list()).set_index("datetime")
            for breakdowns in ungrouped_production_breakdowns
            if len(breakdowns.events) > 0
        ]
        df = pd.concat(prod_and_storage_dfs)
        zoneKey, sources, source_type = ProductionBreakdownList.get_unique_zone_source(
            df
        )
        df = df.groupby(level=0, dropna=False).sum(numeric_only=True, min_count=1)

        for row in df.iterrows():
            production_mix = ProductionMix()
            storage_mix = StorageMix()
            for key, value in row[1].items():
                if np.isnan(value):
                    value = None
                # The key is in the form of "production.<mode>" or "storage.<mode>"
                prefix, mode = key.split(".")  # type: ignore
                if prefix == "production":
                    production_mix.set_value(mode, value)
                elif prefix == "storage":
                    storage_mix.set_value(mode, value)
            production_breakdowns.append(
                zoneKey,
                row[0].to_pydatetime(),  # type: ignore
                sources,
                production_mix,
                storage_mix,
                source_type,
            )
        return production_breakdowns


class TotalProductionList(EventList):
    events: List[TotalProduction]

    def append(
        self,
        zoneKey: ZoneKey,
        datetime: datetime,
        source: str,
        value: float,
        sourceType: EventSourceType = EventSourceType.measured,
    ):
        event = TotalProduction.create(
            self.logger, zoneKey, datetime, source, value, sourceType
        )
        if event:
            self.events.append(event)


class TotalConsumptionList(EventList):
    events: List[TotalConsumption]

    def append(
        self,
        zoneKey: ZoneKey,
        datetime: datetime,
        source: str,
        consumption: float,
        sourceType: EventSourceType = EventSourceType.measured,
    ):
        event = TotalConsumption.create(
            self.logger, zoneKey, datetime, source, consumption, sourceType
        )
        if event:
            self.events.append(event)


class PriceList(EventList):
    events: List[Price]

    def append(
        self,
        zoneKey: ZoneKey,
        datetime: datetime,
        source: str,
        price: float,
        currency: str,
        sourceType: EventSourceType = EventSourceType.measured,
    ):
        event = Price.create(
            self.logger, zoneKey, datetime, source, price, currency, sourceType
        )
        if event:
            self.events.append(event)
