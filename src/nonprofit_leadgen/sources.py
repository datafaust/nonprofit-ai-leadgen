"""Fixed, documented public dataset interfaces."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Source:
    key: str
    dataset_id: str
    page_url: str
    required: tuple[str, ...]

    @property
    def csv_url(self) -> str:
        return f"https://data.cityofnewyork.us/resource/{self.dataset_id}.csv?%24select=%2A%2C%3Aid&%24limit=50000"

SOURCES = (
    Source("community_based", "i4kb-6ab6", "https://data.cityofnewyork.us/Social-Services/NYC-Community-Based-Organizations/i4kb-6ab6", ("organization_name", "street_address", "borough", ":id")),
    Source("womens_resource", "pqg4-dm6b", "https://data.cityofnewyork.us/d/pqg4-dm6b", ("organizationname", "nonprofit", "address1", ":id")),
)
