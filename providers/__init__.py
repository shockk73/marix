from .mogilevminsk import MogilevMinskProvider
from .avto_slava import AvtoSlavaProvider
from .buspro import BusProProvider, MagnitPlusProvider
from .atlasbus import AtlasBusProvider
from .baranovichi_express import BaranovichiExpressProvider

PROVIDERS: dict[str, object] = {
    "mogilevminsk": MogilevMinskProvider(),
    "avto_slava": AvtoSlavaProvider(),
    "buspro": BusProProvider(),
    "magnitplus": MagnitPlusProvider(),
    "atlasbus": AtlasBusProvider(),
    "baranovichi_express": BaranovichiExpressProvider(),
}
