from .mogilevminsk import MogilevMinskProvider
from .avto_slava import AvtoSlavaProvider
from .buspro import BusProProvider, MagnitPlusProvider
from .atlasbus import AtlasBusProvider

PROVIDERS: dict[str, object] = {
    "mogilevminsk": MogilevMinskProvider(),
    "avto_slava": AvtoSlavaProvider(),
    "buspro": BusProProvider(),
    "magnitplus": MagnitPlusProvider(),
    "atlasbus": AtlasBusProvider(),
}
