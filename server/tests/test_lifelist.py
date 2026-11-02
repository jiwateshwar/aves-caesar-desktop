from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from aves_server.catalog import SpeciesCatalog
from aves_server.lifelist import LifeListError, import_life_list, life_species_ids, parse_ebird_csv
from aves_server.models import LifeListEntry
from aves_server.serializers import species_dict


SPECIES_CSV = Path(__file__).parent / "data" / "species.csv"
# Same columns and quirks as a real eBird "World life list" export.
EBIRD = """Row #,Taxon Order,Category,Common Name,Scientific Name,Count,Location,S/P,Date,LocID,SubID,Exotic,Countable
1,29276,species,Verditer Flycatcher,Eumyias thalassinus,2,Bhoj Nagar-Chakki Mod Rd 3,IN-HP,23 Jan 2021,L15812928,S97502458,,1
2,9981,species,Common Kingfisher,Alcedo dubia,1,"Bharatpur--Keoladeo Ghana NP",IN-RJ,01 Nov 2018,L946706,S97307141,,1
3,7731,species,Little Heron,Butorides atricapilla,1,Bharatpur--Keoladeo Ghana NP,IN-RJ,01 Nov 2018,L946706,S97307141,,1
47,814,spuh,duck sp.,Anatidae (duck sp.),1,Ropar,IN-PB,22 Nov 2025,L16584753,S285689490,,0
51,7467,slash,Little/Indian Cormorant,Microcarbo niger/Phalacrocorax fuscicollis,2,Rupnagar,IN-PB,23 Mar 2025,L5156532,S220272913,,0
93,12834,hybrid,Fischer's x Yellow-collared Lovebird (hybrid),Agapornis fischeri x personatus,8,Lake Naivasha,KE-700,11 Sep 2024,L3645588,S201981763,N,0
"""


@pytest.fixture
def catalog(database) -> SpeciesCatalog:
    catalog = SpeciesCatalog(SPECIES_CSV)
    with database.session() as db:
        catalog.seed(db)
    return catalog


def test_only_countable_species_are_read():
    rows = parse_ebird_csv(EBIRD.encode())
    assert [r.common_name for r in rows] == ["Verditer Flycatcher", "Common Kingfisher", "Little Heron"]
    assert (rows[0].first_seen, rows[0].region, rows[1].location) == (date(2021, 1, 23), "IN-HP", "Bharatpur--Keoladeo Ghana NP")


def test_species_are_matched_by_scientific_then_common_name(database, catalog):
    with database.session() as db:
        summary = import_life_list(db, "ebird_world_life_list.csv", parse_ebird_csv(EBIRD.encode()), catalog)
    assert (summary.species, summary.matched) == (3, 2)
    assert summary.unmatched == [{"common_name": "Little Heron", "scientific_name": "Butorides atricapilla"}]
    with database.session() as db:
        matched = {e.common_name: (e.species_id, e.matched_by) for e in db.scalars(select(LifeListEntry))}
        # "Alcedo dubia" isn't the model's name, but "Common Kingfisher" is: a genus or epithet change.
        assert matched == {"Verditer Flycatcher": ("101", "scientific"), "Common Kingfisher": ("102", "common"), "Little Heron": (None, None)}
        assert life_species_ids(db) == {"101", "102"}


def test_a_new_upload_replaces_the_old_one_and_marks_new_species(database, catalog):
    with database.session() as db:
        assert life_species_ids(db) is None
        assert "new" not in species_dict(catalog, "103", "en", life_species_ids(db))  # no list: nothing is "new"
        import_life_list(db, "old.csv", parse_ebird_csv(EBIRD.encode()), catalog)
        only_heron = EBIRD.splitlines()[0] + "\n1,7803,species,Gray Heron,Ardea cinerea,1,Aberdare,KE-200,27 Apr 2013,L1,S1,,1\n"
        import_life_list(db, "new.csv", parse_ebird_csv(only_heron.encode()), catalog)
        life = life_species_ids(db)
        assert life == {"103"}
        assert species_dict(catalog, "103", "en", life)["new"] is False
        assert species_dict(catalog, "101", "en", life)["new"] is True


def test_files_that_are_not_life_lists_are_refused():
    with pytest.raises(LifeListError):
        parse_ebird_csv(b"name,date\nfoo,bar\n")
    with pytest.raises(LifeListError):
        parse_ebird_csv(EBIRD.splitlines()[0].encode() + b"\n47,814,spuh,duck sp.,Anatidae,1,X,IN,22 Nov 2025,L,S,,0\n")
