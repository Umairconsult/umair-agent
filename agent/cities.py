"""Big cities per country (name, latitude, longitude). The agent searches
around each city, one city + one niche at a time, and remembers what it has
already covered so it never repeats the same search for 60 days."""

CITIES = {
 "US": [("New York",40.7128,-74.0060),("Los Angeles",34.0522,-118.2437),("Chicago",41.8781,-87.6298),("Houston",29.7604,-95.3698),("Phoenix",33.4484,-112.0740),("Philadelphia",39.9526,-75.1652),("San Antonio",29.4241,-98.4936),("San Diego",32.7157,-117.1611),("Dallas",32.7767,-96.7970),("Austin",30.2672,-97.7431),("Jacksonville",30.3322,-81.6557),("Fort Worth",32.7555,-97.3308),("Columbus",39.9612,-82.9988),("Charlotte",35.2271,-80.8431),("San Francisco",37.7749,-122.4194),("Indianapolis",39.7684,-86.1581),("Seattle",47.6062,-122.3321),("Denver",39.7392,-104.9903),("Washington DC",38.9072,-77.0369),("Boston",42.3601,-71.0589),("Nashville",36.1627,-86.7816),("Las Vegas",36.1699,-115.1398),("Portland",45.5152,-122.6784),("Atlanta",33.7490,-84.3880),("Miami",25.7617,-80.1918),("Tampa",27.9506,-82.4572),("Orlando",28.5383,-81.3792),("Minneapolis",44.9778,-93.2650),("Detroit",42.3314,-83.0458),("Sacramento",38.5816,-121.4944),("Salt Lake City",40.7608,-111.8910),("Kansas City",39.0997,-94.5786),("St. Louis",38.6270,-90.1994),("Raleigh",35.7796,-78.6382),("Pittsburgh",40.4406,-79.9959),("Cincinnati",39.1031,-84.5120),("Cleveland",41.4993,-81.6944),("Oklahoma City",35.4676,-97.5164),("New Orleans",29.9511,-90.0715),("San Jose",37.3382,-121.8863)],
 "GB": [("London",51.5074,-0.1278),("Birmingham",52.4862,-1.8904),("Manchester",53.4808,-2.2426),("Glasgow",55.8642,-4.2518),("Liverpool",53.4084,-2.9916),("Leeds",53.8008,-1.5491),("Sheffield",53.3811,-1.4701),("Edinburgh",55.9533,-3.1883),("Bristol",51.4545,-2.5879),("Cardiff",51.4816,-3.1791),("Leicester",52.6369,-1.1398),("Nottingham",52.9548,-1.1581),("Newcastle",54.9783,-1.6178),("Southampton",50.9097,-1.4044),("Belfast",54.5973,-5.9301),("Brighton",50.8225,-0.1372),("Reading",51.4543,-0.9781),("Oxford",51.7520,-1.2577),("Cambridge",52.2053,0.1218),("Aberdeen",57.1497,-2.0943)],
 "CA": [("Toronto",43.6532,-79.3832),("Montreal",45.5017,-73.5673),("Vancouver",49.2827,-123.1207),("Calgary",51.0447,-114.0719),("Edmonton",53.5461,-113.4938),("Ottawa",45.4215,-75.6972),("Winnipeg",49.8951,-97.1384),("Quebec City",46.8139,-71.2080),("Hamilton",43.2557,-79.8711),("Halifax",44.6488,-63.5752),("Victoria",48.4284,-123.3656),("Mississauga",43.5890,-79.6441),("London Ontario",42.9849,-81.2453),("Saskatoon",52.1332,-106.6700)],
 "AU": [("Sydney",-33.8688,151.2093),("Melbourne",-37.8136,144.9631),("Brisbane",-27.4698,153.0251),("Perth",-31.9505,115.8605),("Adelaide",-34.9285,138.6007),("Gold Coast",-28.0167,153.4000),("Canberra",-35.2809,149.1300),("Newcastle AU",-32.9283,151.7817),("Hobart",-42.8821,147.3272),("Wollongong",-34.4278,150.8931),("Geelong",-38.1499,144.3617),("Sunshine Coast",-26.6500,153.0667),("Townsville",-19.2590,146.8169),("Darwin",-12.4634,130.8456)],
 "IE": [("Dublin",53.3498,-6.2603),("Cork",51.8985,-8.4756),("Galway",53.2707,-9.0568),("Limerick",52.6638,-8.6267),("Waterford",52.2593,-7.1101)],
 "DE": [("Berlin",52.5200,13.4050),("Hamburg",53.5511,9.9937),("Munich",48.1351,11.5820),("Cologne",50.9375,6.9603),("Frankfurt",50.1109,8.6821),("Stuttgart",48.7758,9.1829),("Dusseldorf",51.2277,6.7735),("Leipzig",51.3397,12.3731),("Dortmund",51.5136,7.4653),("Essen",51.4556,7.0116),("Bremen",53.0793,8.8017),("Dresden",51.0504,13.7373),("Hannover",52.3759,9.7320),("Nuremberg",49.4521,11.0767)],
 "FR": [("Paris",48.8566,2.3522),("Marseille",43.2965,5.3698),("Lyon",45.7640,4.8357),("Toulouse",43.6047,1.4442),("Nice",43.7102,7.2620),("Nantes",47.2184,-1.5536),("Strasbourg",48.5734,7.7521),("Montpellier",43.6108,3.8767),("Bordeaux",44.8378,-0.5792),("Lille",50.6292,3.0573)],
 "ES": [("Madrid",40.4168,-3.7038),("Barcelona",41.3851,2.1734),("Valencia",39.4699,-0.3763),("Seville",37.3891,-5.9845),("Zaragoza",41.6488,-0.8891),("Malaga",36.7213,-4.4214),("Bilbao",43.2630,-2.9350),("Alicante",38.3452,-0.4810),("Palma",39.5696,2.6502)],
 "IT": [("Rome",41.9028,12.4964),("Milan",45.4642,9.1900),("Naples",40.8518,14.2681),("Turin",45.0703,7.6869),("Bologna",44.4949,11.3426),("Florence",43.7696,11.2558),("Genoa",44.4056,8.9463),("Palermo",38.1157,13.3615),("Verona",45.4384,10.9916)],
 "NL": [("Amsterdam",52.3676,4.9041),("Rotterdam",51.9244,4.4777),("The Hague",52.0705,4.3007),("Utrecht",52.0907,5.1214),("Eindhoven",51.4416,5.4697),("Groningen",53.2194,6.5665)],
 "BE": [("Brussels",50.8503,4.3517),("Antwerp",51.2194,4.4025),("Ghent",51.0543,3.7174),("Liege",50.6326,5.5797)],
 "SE": [("Stockholm",59.3293,18.0686),("Gothenburg",57.7089,11.9746),("Malmo",55.6050,13.0038),("Uppsala",59.8586,17.6389)],
 "DK": [("Copenhagen",55.6761,12.5683),("Aarhus",56.1629,10.2039),("Odense",55.4038,10.4024)],
 "NO": [("Oslo",59.9139,10.7522),("Bergen",60.3913,5.3221),("Trondheim",63.4305,10.3951),("Stavanger",58.9700,5.7331)],
 "FI": [("Helsinki",60.1699,24.9384),("Espoo",60.2055,24.6559),("Tampere",61.4978,23.7610),("Turku",60.4518,22.2666)],
 "AT": [("Vienna",48.2082,16.3738),("Graz",47.0707,15.4395),("Salzburg",47.8095,13.0550),("Linz",48.3069,14.2858),("Innsbruck",47.2692,11.4041)],
 "CH": [("Zurich",47.3769,8.5417),("Geneva",46.2044,6.1432),("Basel",47.5596,7.5886),("Bern",46.9480,7.4474),("Lausanne",46.5197,6.6323)],
 "PT": [("Lisbon",38.7223,-9.1393),("Porto",41.1579,-8.6291),("Braga",41.5454,-8.4265),("Faro",37.0194,-7.9304)],
 "PL": [("Warsaw",52.2297,21.0122),("Krakow",50.0647,19.9450),("Lodz",51.7592,19.4560),("Wroclaw",51.1079,17.0385),("Gdansk",54.3520,18.6466),("Poznan",52.4064,16.9252)],
}

COUNTRY_NAMES = {
 "US":"United States","GB":"United Kingdom","CA":"Canada","AU":"Australia","IE":"Ireland","DE":"Germany","FR":"France",
 "ES":"Spain","IT":"Italy","NL":"Netherlands","BE":"Belgium","SE":"Sweden","DK":"Denmark","NO":"Norway","FI":"Finland",
 "AT":"Austria","CH":"Switzerland","PT":"Portugal","PL":"Poland",
}

# How much each market is favoured when the agent chooses where to search next.
COUNTRY_WEIGHT = {"US": 3.0, "GB": 3.0, "CA": 2.0, "AU": 2.0}
