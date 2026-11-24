from pisobot.collectors.base import Collector
from pisobot.collectors.bazos import Bazos
from pisobot.collectors.bezrealitky import Bezrealitky
from pisobot.collectors.espolubydleni import Espolubydleni
from pisobot.collectors.idnes import Idnes
from pisobot.collectors.ulovdomov import UlovDomov


def build_collectors(http, cfg) -> list[Collector]:
    return [
        Bezrealitky(http),
        UlovDomov(http),
        Espolubydleni(http),
        Idnes(http),
        Bazos(http, cfg.raw["collectors"]["bazos_psc_prefixes"]),
    ]
