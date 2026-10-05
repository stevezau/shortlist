"""Curated starter films for calendar presets.

TMDB identities checked against GroupLens MovieLens links.csv and movies.csv:
https://files.grouplens.org/datasets/movielens/ml-latest.zip
Chunuk Bair uses the TMDB link on https://letterboxd.com/film/chunuk-bair/.
These are editable starter selections, not complete or automatically refreshed lists.
"""

from __future__ import annotations

CURATED_DESCRIPTIONS: dict[str, str] = {
    "star_wars_day": "Selected Star Wars films, from the Skywalker saga to standalone adventures.",
    "star_trek_day": "Star Trek films with the original crew, the next generation and the Kelvin timeline.",
    "james_bond_day": "Selected James Bond films, from Dr. No to No Time to Die.",
    "earth_day": "Nature, wildlife and conservation films about our planet and the lives that share it.",
    "pride_month": "Queer stories across romance, comedy, drama and documentary.",
    "women_filmmakers": "Selected films directed or co-directed by women, across genres and generations.",
    "black_history_us": "Films by Black directors across history, everyday life, romance, horror and adventure.",
    "black_history_uk": "Films by Black directors across history, everyday life, romance, horror and adventure.",
    "anzac_day": "Australian and New Zealand stories of service, sacrifice and the aftermath of war.",
    "remembrance_day": "Films reflecting on war, loss, survival and the lives changed by conflict.",
}

# TMDB id, display title, original release year.
CURATED_FILMS: dict[str, tuple[tuple[int, str, int], ...]] = {
    # https://www.starwars.com/films
    # https://www.starwars.com/star-wars-day
    "star_wars_day": (
        (11, "Star Wars", 1977),
        (1891, "The Empire Strikes Back", 1980),
        (1892, "Return of the Jedi", 1983),
        (1893, "Star Wars: Episode I - The Phantom Menace", 1999),
        (1894, "Star Wars: Episode II - Attack of the Clones", 2002),
        (1895, "Star Wars: Episode III - Revenge of the Sith", 2005),
        (12180, "Star Wars: The Clone Wars", 2008),
        (140607, "Star Wars: The Force Awakens", 2015),
        (330459, "Rogue One: A Star Wars Story", 2016),
        (181808, "Star Wars: The Last Jedi", 2017),
        (348350, "Solo: A Star Wars Story", 2018),
        (181812, "Star Wars: The Rise of Skywalker", 2019),
    ),
    # https://www.startrek.com/series-and-movies
    "star_trek_day": (
        (152, "Star Trek: The Motion Picture", 1979),
        (154, "Star Trek II: The Wrath of Khan", 1982),
        (157, "Star Trek III: The Search for Spock", 1984),
        (168, "Star Trek IV: The Voyage Home", 1986),
        (172, "Star Trek V: The Final Frontier", 1989),
        (174, "Star Trek VI: The Undiscovered Country", 1991),
        (193, "Star Trek Generations", 1994),
        (199, "Star Trek: First Contact", 1996),
        (200, "Star Trek: Insurrection", 1998),
        (201, "Star Trek: Nemesis", 2002),
        (13475, "Star Trek", 2009),
        (54138, "Star Trek Into Darkness", 2013),
        (188927, "Star Trek Beyond", 2016),
    ),
    # https://www.007.com/the-films/
    # https://www.007.com/global-james-bond-day/
    "james_bond_day": (
        (646, "Dr. No", 1962),
        (657, "From Russia with Love", 1963),
        (658, "Goldfinger", 1964),
        (660, "Thunderball", 1965),
        (667, "You Only Live Twice", 1967),
        (668, "On Her Majesty's Secret Service", 1969),
        (681, "Diamonds Are Forever", 1971),
        (253, "Live and Let Die", 1973),
        (682, "The Man with the Golden Gun", 1974),
        (691, "The Spy Who Loved Me", 1977),
        (698, "Moonraker", 1979),
        (699, "For Your Eyes Only", 1981),
        (700, "Octopussy", 1983),
        (707, "A View to a Kill", 1985),
        (708, "The Living Daylights", 1987),
        (709, "Licence to Kill", 1989),
        (710, "GoldenEye", 1995),
        (714, "Tomorrow Never Dies", 1997),
        (36643, "The World Is Not Enough", 1999),
        (36669, "Die Another Day", 2002),
        (36557, "Casino Royale", 2006),
        (10764, "Quantum of Solace", 2008),
        (37724, "Skyfall", 2012),
        (206647, "Spectre", 2015),
        (370172, "No Time to Die", 2021),
    ),
    # https://nature.disney.com/
    # https://impact.disney.com/impact-stories/environmental-sustainability-and-nature/the-walt-disney-company-and-national-geographic-celebrate-earth-month/
    "earth_day": (
        (10946, "Earth", 2007),
        (36970, "Oceans", 2009),
        (1667, "March of the Penguins", 2005),
        (72334, "Chimpanzee", 2012),
        (214314, "Bears", 2014),
        (355277, "Born in China", 2016),
        (23128, "The Cove", 2009),
        (1781, "An Inconvenient Truth", 2006),
        (158999, "Blackfish", 2013),
        (263614, "Virunga", 2014),
        (682110, "My Octopus Teacher", 2020),
        (664280, "David Attenborough: A Life on Our Planet", 2020),
    ),
    # https://www.bfi.org.uk/lists/all-voters-votes-30-best-lgbt-films-all-time
    # https://www.loc.gov/lgbt-pride-month/about/
    "pride_month": (
        (142, "Brokeback Mountain", 2005),
        (376867, "Moonlight", 2016),
        (258480, "Carol", 2015),
        (234200, "Pride", 2014),
        (531428, "Portrait of a Lady on Fire", 2019),
        (31225, "Paris Is Burning", 1990),
        (308084, "Tangerine", 2015),
        (20770, "But I'm a Cheerleader", 1999),
        (11000, "The Birdcage", 1996),
        (2759, "The Adventures of Priscilla, Queen of the Desert", 1994),
        (10139, "Milk", 2008),
        (449176, "Love, Simon", 2018),
        (44479, "The Watermelon Woman", 1996),
        (428493, "God's Own Country", 2017),
    ),
    # https://www.un.org/en/observances/womens-day
    "women_filmmakers": (
        (391713, "Lady Bird", 2017),
        (331482, "Little Women", 2019),
        (346698, "Barbie", 2023),
        (713, "The Piano", 1993),
        (12162, "The Hurt Locker", 2008),
        (153, "Lost in Translation", 2003),
        (273895, "Selma", 2014),
        (1088, "Whale Rider", 2002),
        (2011, "Persepolis", 2007),
        (9603, "Clueless", 1995),
        (11287, "A League of Their Own", 1992),
        (581734, "Nomadland", 2020),
        (455, "Bend It Like Beckham", 2002),
        (565310, "The Farewell", 2019),
        (531428, "Portrait of a Lady on Fire", 2019),
    ),
    # https://www.bfi.org.uk/black-history-month
    # https://www.blackhistorymonth.gov/About.html
    "black_history_us": (
        (925, "Do the Right Thing", 1989),
        (1883, "Malcolm X", 1992),
        (376867, "Moonlight", 2016),
        (419430, "Get Out", 2017),
        (284054, "Black Panther", 2018),
        (157354, "Fruitvale Station", 2013),
        (650, "Boyz n the Hood", 1991),
        (14736, "Love & Basketball", 2000),
        (465914, "If Beale Street Could Talk", 2018),
        (583406, "Judas and the Black Messiah", 2021),
        (424781, "Sorry to Bother You", 2018),
        (68427, "Daughters of the Dust", 1991),
        (661914, "One Night in Miami...", 2020),
        (407806, "13th", 2016),
        (76203, "12 Years a Slave", 2013),
        (95597, "Black Girl", 1966),
    ),
    # https://www.bfi.org.uk/black-history-month
    "black_history_uk": (
        (925, "Do the Right Thing", 1989),
        (1883, "Malcolm X", 1992),
        (376867, "Moonlight", 2016),
        (419430, "Get Out", 2017),
        (284054, "Black Panther", 2018),
        (157354, "Fruitvale Station", 2013),
        (650, "Boyz n the Hood", 1991),
        (14736, "Love & Basketball", 2000),
        (465914, "If Beale Street Could Talk", 2018),
        (583406, "Judas and the Black Messiah", 2021),
        (424781, "Sorry to Bother You", 2018),
        (68427, "Daughters of the Dust", 1991),
        (661914, "One Night in Miami...", 2020),
        (407806, "13th", 2016),
        (76203, "12 Years a Slave", 2013),
        (95597, "Black Girl", 1966),
    ),
    # https://www.awm.gov.au/commemoration/anzac-day/traditions
    # https://www.nzfilm.co.nz/films/chunuk-bair
    # https://www.acmi.net.au/works/83991--the-odd-angry-shot/
    "anzac_day": (
        (11646, "Gallipoli", 1981),
        (13783, "Breaker Morant", 1980),
        (18389, "The Lighthorsemen", 1987),
        (43418, "Beneath Hill 60", 2010),
        (508664, "Danger Close: The Battle of Long Tan", 2019),
        (9774, "Kokoda", 2006),
        (256917, "The Water Diviner", 2014),
        (29582, "The Odd Angry Shot", 1979),
        (77223, "Paradise Road", 1997),
        (134104, "Chunuk Bair", 1992),
    ),
    # https://www.awm.gov.au/commemoration/remembrance-day
    "remembrance_day": (
        (143, "All Quiet on the Western Front", 1930),
        (49046, "All Quiet on the Western Front", 2022),
        (530915, "1917", 2019),
        (975, "Paths of Glory", 1957),
        (543580, "They Shall Not Grow Old", 2018),
        (12477, "Grave of the Fireflies", 1988),
        (25237, "Come and See", 1985),
        (8741, "The Thin Red Line", 1998),
        (887, "The Best Years of Our Lives", 1946),
        (284689, "Testament of Youth", 2014),
        (4347, "Atonement", 2007),
        (11661, "Joyeux Noël", 2005),
        (1251, "Letters from Iwo Jima", 2006),
        (57212, "War Horse", 2011),
    ),
}
