# Phase D college alias gaps

Built from the read-only live snapshot in `/home/ubuntu/work/phase_d_live_inputs.json` with `scripts/build_team_aliases.py`. A direct participant alias is written only when the normalized token sets are exactly equal and resolve to one participant. A unique same-date pairing with both opponents resolving is also accepted. Reused sport-level codes with different team assignments and all other ambiguous candidates remain unmapped.

## Coverage

| Kalshi series | Sport | Before (codes seen, mapped) | After (codes seen, mapped) |
|---|---|---:|---:|
| `KXNCAAFGAME` | `americanfootball_ncaaf` | (266, 109) | (266, 111) |
| `KXNCAAMBGAME` | `basketball_ncaab` | (4, 0) | (4, 0) |
| `KXUELGAME` | `soccer_uefa_europa_league` | (34, 0) | (34, 32) |
| `KXUECLGAME` | `soccer_uefa_europa_conference_league` | (36, 0) | (36, 17) |

## Unmapped current market codes

Candidate names and scores are diagnostics only; none are aliases in `teams.json`.

### `KXNCAAFGAME`

- `AAMU` — market name(s): Alabama A&M; candidate participant(s): Alabama A&M Bulldogs (0.750), Florida A&M Rattlers (0.400), Texas A&M Aggies (0.400), East Texas A&M Lions (0.333), Prairie View A&M Panthers (0.333). No unique normalized participant match or unique kickoff pairing.
- `AC` — market name(s): Abilene Christian; candidate participant(s): Abilene Christian Wildcats (0.667). No unique normalized participant match or unique kickoff pairing.
- `ALCN` — market name(s): Alcorn St.; candidate participant(s): Alcorn State Braves (0.500). No unique normalized participant match or unique kickoff pairing.
- `ALST` — market name(s): Alabama St.; candidate participant(s): Alabama State Hornets (0.500), Alabama A&M Bulldogs (0.340), Alabama Crimson Tide (0.340), North Alabama Lions (0.340), South Alabama Jaguars (0.340). No unique normalized participant match or unique kickoff pairing.
- `APP` — market name(s): Appalachian St.; candidate participant(s): Appalachian State Mountaineers (0.500). No unique normalized participant match or unique kickoff pairing.
- `ARPB` — market name(s): Arkansas-Pine Bluff; candidate participant(s): Arkansas Pine Bluff Golden Lions (0.500), Arkansas Razorbacks (0.200), Arkansas State Red Wolves (0.167), Central Arkansas Bears (0.167). No unique normalized participant match or unique kickoff pairing.
- `BRWN` — market name(s): Brown; candidate participant(s): Brown Bears (0.500). No unique normalized participant match or unique kickoff pairing.
- `BRY` — market name(s): Bryant; candidate participant(s): Bryant Bulldogs (0.500). No unique normalized participant match or unique kickoff pairing.
- `BUCK` — market name(s): Bucknell; candidate participant(s): Bucknell Bison (0.500). No unique normalized participant match or unique kickoff pairing.
- `BUT` — market name(s): Butler; candidate participant(s): Butler Bulldogs (0.500). No unique normalized participant match or unique kickoff pairing.
- `CAMP` — market name(s): Campbell; candidate participant(s): Campbell Fighting Camels (0.340). No unique normalized participant match or unique kickoff pairing.
- `CARK` — market name(s): Central Arkansas; candidate participant(s): Central Arkansas Bears (0.667), Arkansas Razorbacks (0.333), Arkansas State Red Wolves (0.250), Central Michigan Chippewas (0.250), Central Connecticut Blue Devils (0.200). No unique normalized participant match or unique kickoff pairing.
- `CCAR` — market name(s): Coastal Carolina; candidate participant(s): Coastal Carolina Chanticleers (0.667), East Carolina Pirates (0.250), South Carolina Gamecocks (0.250), South Carolina State Bulldogs (0.250), Western Carolina Catamounts (0.250). No unique normalized participant match or unique kickoff pairing.
- `CCSU` — market name(s): Central Connecticut St.; candidate participant(s): Central Connecticut Blue Devils (0.500), Central Arkansas Bears (0.250), Central Michigan Chippewas (0.250), North Carolina Central Eagles (0.200). No unique normalized participant match or unique kickoff pairing.
- `CHAR` — market name(s): Charlotte; candidate participant(s): Charlotte 49ers (0.500). No unique normalized participant match or unique kickoff pairing.
- `CHAT` — market name(s): Chattanooga; candidate participant(s): Chattanooga Mocs (0.500). No unique normalized participant match or unique kickoff pairing.
- `CHS` — market name(s): Chicago St.; candidate participant(s): Chicago State Cougars (0.500). No unique normalized participant match or unique kickoff pairing.
- `CHSO` — market name(s): Charleston Southern; candidate participant(s): Charleston Southern Buccaneers (0.667), Southern University Jaguars (0.333), Georgia Southern Eagles (0.250), Southern Illinois Salukis (0.250), Southern Utah Thunderbirds (0.250). No unique normalized participant match or unique kickoff pairing.
- `CIT` — market name(s): The Citadel; candidate participant(s): Citadel Bulldogs (0.500). No unique normalized participant match or unique kickoff pairing.
- `CLMB` — market name(s): Columbia; candidate participant(s): Columbia Lions (0.500). No unique normalized participant match or unique kickoff pairing.
- `COLG` — market name(s): Colgate; candidate participant(s): Colgate Raiders (0.500). No unique normalized participant match or unique kickoff pairing.
- `COOK` — market name(s): Bethune-Cookman; candidate participant(s): Bethune-Cookman Wildcats (0.750). No unique normalized participant match or unique kickoff pairing.
- `COR` — market name(s): Cornell; candidate participant(s): Cornell Big Red (0.340). No unique normalized participant match or unique kickoff pairing.
- `CP` — market name(s): Cal Poly; candidate participant(s): Cal Poly Mustangs (0.667). No unique normalized participant match or unique kickoff pairing.
- `DART` — market name(s): Dartmouth; candidate participant(s): Dartmouth Big Green (0.340). No unique normalized participant match or unique kickoff pairing.
- `DAV` — market name(s): Davidson; candidate participant(s): Davidson Wildcats (0.500). No unique normalized participant match or unique kickoff pairing.
- `DAY` — market name(s): Dayton; candidate participant(s): Dayton Flyers (0.500). No unique normalized participant match or unique kickoff pairing.
- `DRKE` — market name(s): Drake; candidate participant(s): Drake Bulldogs (0.500). No unique normalized participant match or unique kickoff pairing.
- `DSU` — market name(s): Delaware St.; candidate participant(s): Delaware State Hornets (0.500), Delaware Blue Hens (0.340). No unique normalized participant match or unique kickoff pairing.
- `DUQ` — market name(s): Duquesne; candidate participant(s): Duquesne Dukes (0.500). No unique normalized participant match or unique kickoff pairing.
- `ECU` — market name(s): East Carolina; candidate participant(s): East Carolina Pirates (0.667), Coastal Carolina Chanticleers (0.250), East Tennessee State Buccaneers (0.250), South Carolina Gamecocks (0.250), South Carolina State Bulldogs (0.250). No unique normalized participant match or unique kickoff pairing.
- `EIU` — market name(s): Eastern Illinois; candidate participant(s): Eastern Illinois Panthers (0.667), Western Illinois Leathernecks (0.667), Illinois State Redbirds (0.333), Eastern Kentucky Colonels (0.250), Eastern Michigan Eagles (0.250). No unique normalized participant match or unique kickoff pairing.
- `EKY` — market name(s): Eastern Kentucky; candidate participant(s): Eastern Kentucky Colonels (0.667), Western Kentucky Hilltoppers (0.667), Kentucky State University Thorobreds (0.333), Kentucky Wildcats (0.333), Eastern Illinois Panthers (0.250). No unique normalized participant match or unique kickoff pairing.
- `ELON` — market name(s): Elon; candidate participant(s): Elon Phoenix (0.500). No unique normalized participant match or unique kickoff pairing.
- `ETAM` — market name(s): East Texas A&M; candidate participant(s): East Texas A&M Lions (0.800), Texas A&M Aggies (0.600), Alabama A&M Bulldogs (0.333), Florida A&M Rattlers (0.333), Prairie View A&M Panthers (0.286). No unique normalized participant match or unique kickoff pairing.
- `ETSU` — market name(s): East Tennessee St.; candidate participant(s): East Tennessee State Buccaneers (0.667), Tennessee State Tigers (0.333), Tennessee Volunteers (0.333), East Carolina Pirates (0.250), Middle Tennessee Blue Raiders (0.200). No unique normalized participant match or unique kickoff pairing.
- `EWU` — market name(s): Eastern Washington; candidate participant(s): Eastern Washington Eagles (0.667), Washington Huskies (0.333), Washington State Cougars (0.333), Eastern Illinois Panthers (0.250), Eastern Kentucky Colonels (0.250). No unique normalized participant match or unique kickoff pairing.
- `FAMU` — market name(s): Florida A&M; candidate participant(s): Florida A&M Rattlers (0.750), Alabama A&M Bulldogs (0.400), Texas A&M Aggies (0.400), East Texas A&M Lions (0.333), Prairie View A&M Panthers (0.333). No unique normalized participant match or unique kickoff pairing.
- `FIU` — market name(s): Florida International; candidate participant(s): Florida International Panthers (0.667), Florida Gators (0.333), Florida State Seminoles (0.333), Florida Atlantic Owls (0.250), South Florida Bulls (0.250). No unique normalized participant match or unique kickoff pairing.
- `FOR` — market name(s): Fordham; candidate participant(s): Fordham Rams (0.500). No unique normalized participant match or unique kickoff pairing.
- `FSU` — market name(s): Florida St.; candidate participant(s): Florida Gators (0.500), Florida State Seminoles (0.500), Florida A&M Rattlers (0.340), Florida Atlantic Owls (0.340), Florida International Panthers (0.340). No unique normalized participant match or unique kickoff pairing.
- `FUR` — market name(s): Furman; candidate participant(s): Furman Paladins (0.500). No unique normalized participant match or unique kickoff pairing.
- `GASO` — market name(s): Georgia Southern; candidate participant(s): Georgia Southern Eagles (0.667), Georgia Bulldogs (0.333), Georgia State Panthers (0.333), Southern University Jaguars (0.333), Charleston Southern Buccaneers (0.250). No unique normalized participant match or unique kickoff pairing.
- `GRAM` — market name(s): Grambling St.; candidate participant(s): Grambling State Tigers (0.500). No unique normalized participant match or unique kickoff pairing.
- `GTWN` — market name(s): Georgetown; candidate participant(s): Georgetown Hoyas (0.500). No unique normalized participant match or unique kickoff pairing.
- `HAMP` — market name(s): Hampton; candidate participant(s): Hampton Pirates (0.500). No unique normalized participant match or unique kickoff pairing.
- `HARV` — market name(s): Harvard; candidate participant(s): Harvard Crimson (0.500). No unique normalized participant match or unique kickoff pairing.
- `HC` — market name(s): Holy Cross; candidate participant(s): Holy Cross Crusaders (0.667). No unique normalized participant match or unique kickoff pairing.
- `HCU` — market name(s): Houston Christian; candidate participant(s): Houston Cougars (0.333), Abilene Christian Wildcats (0.250), Houston Baptist Huskies (0.250), Sam Houston State Bearkats (0.250). No unique normalized participant match or unique kickoff pairing.
- `HOW` — market name(s): Howard; candidate participant(s): Howard Bison (0.500). No unique normalized participant match or unique kickoff pairing.
- `IDHO` — market name(s): Idaho; candidate participant(s): Idaho State Bengals (0.500), Idaho Vandals (0.500). No unique normalized participant match or unique kickoff pairing.
- `IDST` — market name(s): Idaho St.; candidate participant(s): Idaho State Bengals (0.500), Idaho Vandals (0.500). No unique normalized participant match or unique kickoff pairing.
- `ILST` — market name(s): Illinois St.; candidate participant(s): Illinois State Redbirds (0.500), Eastern Illinois Panthers (0.340), Illinois Fighting Illini (0.340), Northern Illinois Huskies (0.340), Southern Illinois Salukis (0.340). No unique normalized participant match or unique kickoff pairing.
- `INST` — market name(s): Indiana St.; candidate participant(s): Indiana Hoosiers (0.500), Indiana State Sycamores (0.500). No unique normalized participant match or unique kickoff pairing.
- `IW` — market name(s): Incarnate Word; candidate participant(s): Incarnate Word Cardinals (0.667). No unique normalized participant match or unique kickoff pairing.
- `JKST` — market name(s): Jackson St.; candidate participant(s): Jackson State Tigers (0.500). No unique normalized participant match or unique kickoff pairing.
- `JMU` — market name(s): James Madison; candidate participant(s): James Madison Dukes (0.667). No unique normalized participant match or unique kickoff pairing.
- `KENN` — market name(s): Kennesaw St.; candidate participant(s): Kennesaw State Owls (0.500). No unique normalized participant match or unique kickoff pairing.
- `LAF` — market name(s): Lafayette; candidate participant(s): Lafayette Leopards (0.500). No unique normalized participant match or unique kickoff pairing.
- `LAM` — market name(s): Lamar; candidate participant(s): Lamar Cardinals (0.500). No unique normalized participant match or unique kickoff pairing.
- `LEH` — market name(s): Lehigh; candidate participant(s): Lehigh Mountain Hawks (0.340). No unique normalized participant match or unique kickoff pairing.
- `LIB` — market name(s): Liberty; candidate participant(s): Liberty Flames (0.500). No unique normalized participant match or unique kickoff pairing.
- `LINW` — market name(s): Lindenwood; candidate participant(s): Lindenwood Lions (0.500). No unique normalized participant match or unique kickoff pairing.
- `LIU` — market name(s): LIU; candidate participant(s): LIU Sharks (0.500). No unique normalized participant match or unique kickoff pairing.
- `LOU` — market name(s): Louisville; candidate participant(s): Louisville Cardinals (0.500). No unique normalized participant match or unique kickoff pairing.
- `MCNS` — market name(s): McNeese; candidate participant(s): McNeese State Cowboys (0.500). No unique normalized participant match or unique kickoff pairing.
- `MD` — market name(s): Maryland; candidate participant(s): Maryland Terrapins (0.500). No unique normalized participant match or unique kickoff pairing.
- `ME` — market name(s): Maine; candidate participant(s): Maine Black Bears (0.340). No unique normalized participant match or unique kickoff pairing.
- `MEM` — market name(s): Memphis; candidate participant(s): Memphis Tigers (0.500). No unique normalized participant match or unique kickoff pairing.
- `MER` — market name(s): Mercer; candidate participant(s): Mercer Bears (0.500). No unique normalized participant match or unique kickoff pairing.
- `MHU` — market name(s): Mercyhurst; candidate participant(s): Mercyhurst Lakers (0.500). No unique normalized participant match or unique kickoff pairing.
- `MONM` — market name(s): Monmouth; candidate participant(s): Monmouth Hawks (0.500). No unique normalized participant match or unique kickoff pairing.
- `MONT` — market name(s): Montana; candidate participant(s): Montana Grizzlies (0.500), Montana State Bobcats (0.500), Lehigh Mountain Hawks (0.340). No unique normalized participant match or unique kickoff pairing.
- `MORE` — market name(s): Morehead St.; candidate participant(s): Morehead State Eagles (0.500). No unique normalized participant match or unique kickoff pairing.
- `MORG` — market name(s): Morgan St.; candidate participant(s): Morgan State Bears (0.500). No unique normalized participant match or unique kickoff pairing.
- `MRMK` — market name(s): Merrimack; candidate participant(s): Merrimack Warriors (0.500). No unique normalized participant match or unique kickoff pairing.
- `MRSH` — market name(s): Marshall; candidate participant(s): Marshall Thundering Herd (0.340). No unique normalized participant match or unique kickoff pairing.
- `MRST` — market name(s): Marist; candidate participant(s): Marist Red Foxes (0.340). No unique normalized participant match or unique kickoff pairing.
- `MTST` — market name(s): Montana St.; candidate participant(s): Montana Grizzlies (0.500), Montana State Bobcats (0.500), Lehigh Mountain Hawks (0.340). No unique normalized participant match or unique kickoff pairing.
- `MTU` — market name(s): Middle Tennessee; candidate participant(s): Middle Tennessee Blue Raiders (0.500), Tennessee State Tigers (0.333), Tennessee Volunteers (0.333), East Tennessee State Buccaneers (0.250), Tennessee Tech Golden Eagles (0.200). No unique normalized participant match or unique kickoff pairing.
- `MURR` — market name(s): Murray St.; candidate participant(s): Murray State Racers (0.500). No unique normalized participant match or unique kickoff pairing.
- `MVSU` — market name(s): Mississippi Valley St.; candidate participant(s): Mississippi Valley State Delta Devils (0.500), Mississippi State Bulldogs (0.333), Southern Mississippi Golden Eagles (0.200), UT Rio Grande Valley Vaqueros (0.167). No unique normalized participant match or unique kickoff pairing.
- `NAU` — market name(s): Northern Arizona; candidate participant(s): Northern Arizona Lumberjacks (0.667), Arizona Wildcats (0.333), Arizona State Sun Devils (0.250), Northern Colorado Bears (0.250), Northern Illinois Huskies (0.250). No unique normalized participant match or unique kickoff pairing.
- `NCAT` — market name(s): North Carolina A&T; candidate participant(s): North Carolina A&T Aggies (0.800), North Carolina Central Eagles (0.333), North Carolina Tar Heels (0.333), Coastal Carolina Chanticleers (0.167), East Carolina Pirates (0.167). No unique normalized participant match or unique kickoff pairing.
- `NCCU` — market name(s): North Carolina Central; candidate participant(s): North Carolina Central Eagles (0.750), North Carolina Tar Heels (0.400), North Carolina A&T Aggies (0.333), Central Arkansas Bears (0.200), Central Michigan Chippewas (0.200). No unique normalized participant match or unique kickoff pairing.
- `NHC` — market name(s): New Haven; candidate participant(s): New Haven Chargers (0.667), New Hampshire Wildcats (0.250), New Mexico Lobos (0.250), New Mexico State Aggies (0.250). No unique normalized participant match or unique kickoff pairing.
- `NICH` — market name(s): Nicholls St.; candidate participant(s): Nicholls State Colonels (0.500). No unique normalized participant match or unique kickoff pairing.
- `NORF` — market name(s): Norfolk St.; candidate participant(s): Norfolk State Spartans (0.500). No unique normalized participant match or unique kickoff pairing.
- `NWST` — market name(s): Northwestern St.; candidate participant(s): Northwestern State Demons (0.500), Northwestern Wildcats (0.500). No unique normalized participant match or unique kickoff pairing.
- `PEAY` — market name(s): Austin Peay; candidate participant(s): Austin Peay Governors (0.667), Stephen F. Austin Lumberjacks (0.200). No unique normalized participant match or unique kickoff pairing.
- `PENN` — market name(s): Penn; candidate participant(s): Penn State Nittany Lions (0.340). No unique normalized participant match or unique kickoff pairing.
- `PRE` — market name(s): Presbyterian; candidate participant(s): Presbyterian College Blue Hose (0.340). No unique normalized participant match or unique kickoff pairing.
- `PRIN` — market name(s): Princeton; candidate participant(s): Princeton Tigers (0.500). No unique normalized participant match or unique kickoff pairing.
- `PRST` — market name(s): Portland St.; candidate participant(s): Portland State Vikings (0.500). No unique normalized participant match or unique kickoff pairing.
- `PSU` — market name(s): Penn St.; candidate participant(s): Penn State Nittany Lions (0.340). No unique normalized participant match or unique kickoff pairing.
- `PUR` — market name(s): Purdue; candidate participant(s): Purdue Boilermakers (0.500). No unique normalized participant match or unique kickoff pairing.
- `PV` — market name(s): Prairie View A&M; candidate participant(s): Prairie View A&M Panthers (0.800), Alabama A&M Bulldogs (0.333), Florida A&M Rattlers (0.333), Texas A&M Aggies (0.333), East Texas A&M Lions (0.286). No unique normalized participant match or unique kickoff pairing.
- `RICH` — market name(s): Richmond; candidate participant(s): Richmond Spiders (0.500). No unique normalized participant match or unique kickoff pairing.
- `RMU` — market name(s): Robert Morris; candidate participant(s): Robert Morris Colonials (0.667). No unique normalized participant match or unique kickoff pairing.
- `SAM` — market name(s): Samford; candidate participant(s): Samford Bulldogs (0.500), Stanford Cardinal (0.500). No unique normalized participant match or unique kickoff pairing.
- `SCST` — market name(s): South Carolina St.; candidate participant(s): South Carolina Gamecocks (0.667), South Carolina State Bulldogs (0.667), Coastal Carolina Chanticleers (0.250), East Carolina Pirates (0.250), South Alabama Jaguars (0.250). No unique normalized participant match or unique kickoff pairing.
- `SDAK` — market name(s): South Dakota; candidate participant(s): South Dakota Coyotes (0.667), South Dakota State Jackrabbits (0.667), North Dakota State Bison (0.250), South Alabama Jaguars (0.250), South Carolina Gamecocks (0.250). No unique normalized participant match or unique kickoff pairing.
- `SDST` — market name(s): South Dakota St.; candidate participant(s): South Dakota Coyotes (0.667), South Dakota State Jackrabbits (0.667), North Dakota State Bison (0.250), South Alabama Jaguars (0.250), South Carolina Gamecocks (0.250). No unique normalized participant match or unique kickoff pairing.
- `SELA` — market name(s): Southeastern Louisiana; candidate participant(s): Southeastern Louisiana Lions (0.667), Louisiana Ragin Cajuns (0.250), Louisiana Tech Bulldogs (0.250). No unique normalized participant match or unique kickoff pairing.
- `SEMO` — market name(s): Southeast Missouri St.; candidate participant(s): Southeast Missouri State Redhawks (0.667), Missouri State Bears (0.333), Missouri Tigers (0.333). No unique normalized participant match or unique kickoff pairing.
- `SFA` — market name(s): Stephen F. Austin; candidate participant(s): Stephen F. Austin Lumberjacks (0.750), Austin Peay Governors (0.200). No unique normalized participant match or unique kickoff pairing.
- `SHU` — market name(s): Sacred Heart; candidate participant(s): Sacred Heart Pioneers (0.667). No unique normalized participant match or unique kickoff pairing.
- `SIU` — market name(s): Southern Illinois; candidate participant(s): Southern Illinois Salukis (0.667), Illinois State Redbirds (0.333), Southern University Jaguars (0.333), Charleston Southern Buccaneers (0.250), Eastern Illinois Panthers (0.250). No unique normalized participant match or unique kickoff pairing.
- `SOU` — market name(s): Southern University; candidate participant(s): Southern University Jaguars (0.500), Charleston Southern Buccaneers (0.340), Georgia Southern Eagles (0.340), Southern Illinois Salukis (0.340), Southern Mississippi Golden Eagles (0.340). No unique normalized participant match or unique kickoff pairing.
- `STET` — market name(s): Stetson; candidate participant(s): Stetson Hatters (0.500). No unique normalized participant match or unique kickoff pairing.
- `STNH` — market name(s): Stonehill; candidate participant(s): Stonehill Skyhawks (0.500). No unique normalized participant match or unique kickoff pairing.
- `STON` — market name(s): Stony Brook; candidate participant(s): Stony Brook Seawolves (0.667). No unique normalized participant match or unique kickoff pairing.
- `SUU` — market name(s): Southern Utah; candidate participant(s): Southern Utah Thunderbirds (0.667), Southern University Jaguars (0.333), Utah State Aggies (0.333), Utah Utes (0.333), Charleston Southern Buccaneers (0.250). No unique normalized participant match or unique kickoff pairing.
- `TARL` — market name(s): Tarleton St.; candidate participant(s): Tarleton State Texans (0.500). No unique normalized participant match or unique kickoff pairing.
- `TEM` — market name(s): Temple; candidate participant(s): Temple Owls (0.500). No unique normalized participant match or unique kickoff pairing.
- `TNST` — market name(s): Tennessee St.; candidate participant(s): Tennessee State Tigers (0.500), Tennessee Volunteers (0.500), East Tennessee State Buccaneers (0.340), Middle Tennessee Blue Raiders (0.340), Tennessee Tech Golden Eagles (0.340). No unique normalized participant match or unique kickoff pairing.
- `TNTC` — market name(s): Tennessee Tech; candidate participant(s): Tennessee Tech Golden Eagles (0.500), Tennessee State Tigers (0.333), Tennessee Volunteers (0.333), East Tennessee State Buccaneers (0.250), Louisiana Tech Bulldogs (0.250). No unique normalized participant match or unique kickoff pairing.
- `TOWS` — market name(s): Towson; candidate participant(s): Towson Tigers (0.500). No unique normalized participant match or unique kickoff pairing.
- `TROY` — market name(s): Troy; candidate participant(s): Troy Trojans (0.500). No unique normalized participant match or unique kickoff pairing.
- `TXSO` — market name(s): Texas Southern; candidate participant(s): Texas Southern Tigers (0.667), Southern University Jaguars (0.333), Tarleton State Texans (0.333), Texas Longhorns (0.333), Texas State Bobcats (0.333). No unique normalized participant match or unique kickoff pairing.
- `UAB` — market name(s): UAB; candidate participant(s): UAB Blazers (0.500). No unique normalized participant match or unique kickoff pairing.
- `UCD` — market name(s): UC Davis; candidate participant(s): UC Davis Aggies (0.667). No unique normalized participant match or unique kickoff pairing.
- `ULL` — market name(s): Louisiana; candidate participant(s): Louisiana Ragin Cajuns (0.340), Louisiana Tech Bulldogs (0.340), Southeastern Louisiana Lions (0.340). No unique normalized participant match or unique kickoff pairing.
- `ULM` — market name(s): Louisiana-Monroe; candidate participant(s): Louisiana Ragin Cajuns (0.200), Louisiana Tech Bulldogs (0.200), Southeastern Louisiana Lions (0.200), UL Monroe Warhawks (0.200). No unique normalized participant match or unique kickoff pairing.
- `UNA` — market name(s): North Alabama; candidate participant(s): North Alabama Lions (0.667), Alabama State Hornets (0.333), Alabama Crimson Tide (0.250), North Dakota State Bison (0.250), South Alabama Jaguars (0.250). No unique normalized participant match or unique kickoff pairing.
- `UNCO` — market name(s): Northern Colorado; candidate participant(s): Northern Colorado Bears (0.667), Colorado Buffaloes (0.333), Colorado State Rams (0.333), Northern Arizona Lumberjacks (0.250), Northern Illinois Huskies (0.250). No unique normalized participant match or unique kickoff pairing.
- `UND` — market name(s): North Dakota; candidate participant(s): North Dakota State Bison (0.667), North Dakota Fighting Hawks (0.500), North Alabama Lions (0.250), South Dakota Coyotes (0.250), South Dakota State Jackrabbits (0.250). No unique normalized participant match or unique kickoff pairing.
- `UNH` — market name(s): New Hampshire; candidate participant(s): New Hampshire Wildcats (0.667), New Haven Chargers (0.250), New Mexico Lobos (0.250), New Mexico State Aggies (0.250). No unique normalized participant match or unique kickoff pairing.
- `UNI` — market name(s): Northern Iowa; candidate participant(s): Northern Iowa Panthers (0.667), Iowa Hawkeyes (0.333), Iowa State Cyclones (0.333), Northern Arizona Lumberjacks (0.250), Northern Colorado Bears (0.250). No unique normalized participant match or unique kickoff pairing.
- `UNLV` — market name(s): UNLV; candidate participant(s): UNLV Rebels (0.500). No unique normalized participant match or unique kickoff pairing.
- `URI` — market name(s): Rhode Island; candidate participant(s): Rhode Island Rams (0.667). No unique normalized participant match or unique kickoff pairing.
- `USA` — market name(s): South Alabama; candidate participant(s): South Alabama Jaguars (0.667), Alabama State Hornets (0.333), Alabama Crimson Tide (0.250), North Alabama Lions (0.250), South Carolina Gamecocks (0.250). No unique normalized participant match or unique kickoff pairing.
- `USD` — market name(s): San Diego; candidate participant(s): San Diego State Aztecs (0.667), San Diego Toreros (0.667), San Jose State Spartans (0.250). No unique normalized participant match or unique kickoff pairing.
- `USF` — market name(s): South Florida; candidate participant(s): South Florida Bulls (0.667), Florida Gators (0.333), Florida State Seminoles (0.333), Florida Atlantic Owls (0.250), Florida International Panthers (0.250). No unique normalized participant match or unique kickoff pairing.
- `USM` — market name(s): Southern Miss; candidate participant(s): Southern University Jaguars (0.333), Charleston Southern Buccaneers (0.250), Georgia Southern Eagles (0.250), Ole Miss Rebels (0.250), Southern Illinois Salukis (0.250). No unique normalized participant match or unique kickoff pairing.
- `UST` — market name(s): St. Thomas; candidate participant(s): St. Thomas (MN) Tommies (0.340). No unique normalized participant match or unique kickoff pairing.
- `UTEP` — market name(s): UTEP; candidate participant(s): UTEP Miners (0.500). No unique normalized participant match or unique kickoff pairing.
- `UTM` — market name(s): Tennessee-Martin; candidate participant(s): Tennessee State Tigers (0.250), Tennessee Volunteers (0.250), East Tennessee State Buccaneers (0.200), UT Martin Skyhawks (0.200), Middle Tennessee Blue Raiders (0.167). No unique normalized participant match or unique kickoff pairing.
- `UTRGV` — market name(s): UT Rio Grande Valley; candidate participant(s): UT Rio Grande Valley Vaqueros (0.800), UT Martin Skyhawks (0.167), Mississippi Valley State Delta Devils (0.143). No unique normalized participant match or unique kickoff pairing.
- `UTU` — market name(s): Utah Tech; candidate participant(s): Utah Tech Trailblazers (0.667), Utah State Aggies (0.333), Utah Utes (0.333), Louisiana Tech Bulldogs (0.250), Southern Utah Thunderbirds (0.250). No unique normalized participant match or unique kickoff pairing.
- `UWFL` — market name(s): West Florida; candidate participant(s): West Florida Argonauts (0.667), Florida Gators (0.333), Florida State Seminoles (0.333), Florida Atlantic Owls (0.250), Florida International Panthers (0.250). No unique normalized participant match or unique kickoff pairing.
- `UWGA` — market name(s): West Georgia; candidate participant(s): West Georgia Wolves (0.667), Georgia Bulldogs (0.333), Georgia State Panthers (0.333), Georgia Southern Eagles (0.250), West Florida Argonauts (0.250). No unique normalized participant match or unique kickoff pairing.
- `VALP` — market name(s): Valparaiso; candidate participant(s): Valparaiso Beacons (0.500). No unique normalized participant match or unique kickoff pairing.
- `VILL` — market name(s): Villanova; candidate participant(s): Villanova Wildcats (0.500). No unique normalized participant match or unique kickoff pairing.
- `VMI` — market name(s): VMI; candidate participant(s): VMI Keydets (0.500). No unique normalized participant match or unique kickoff pairing.
- `WAG` — market name(s): Wagner; candidate participant(s): Wagner Seahawks (0.500). No unique normalized participant match or unique kickoff pairing.
- `WAKE` — market name(s): Wake Forest; candidate participant(s): Wake Forest Demon Deacons (0.500). No unique normalized participant match or unique kickoff pairing.
- `WCU` — market name(s): Western Carolina; candidate participant(s): Western Carolina Catamounts (0.667), Coastal Carolina Chanticleers (0.250), East Carolina Pirates (0.250), Eastern Illinois Panthers (0.250), Eastern Kentucky Colonels (0.250). No unique normalized participant match or unique kickoff pairing.
- `WEB` — market name(s): Weber St.; candidate participant(s): Weber State Wildcats (0.500). No unique normalized participant match or unique kickoff pairing.
- `WEBB` — market name(s): Gardner-Webb; candidate participant(s): Gardner-Webb Runnin Bulldogs (0.600). No unique normalized participant match or unique kickoff pairing.
- `WIU` — market name(s): Western Illinois; candidate participant(s): Eastern Illinois Panthers (0.667), Western Illinois Leathernecks (0.667), Illinois State Redbirds (0.333), Eastern Kentucky Colonels (0.250), Eastern Michigan Eagles (0.250). No unique normalized participant match or unique kickoff pairing.
- `WM` — market name(s): William & Mary; candidate participant(s): William and Mary Tribe (0.667). No unique normalized participant match or unique kickoff pairing.
- `WOF` — market name(s): Wofford; candidate participant(s): Wofford Terriers (0.500). No unique normalized participant match or unique kickoff pairing.
- `YALE` — market name(s): Yale; candidate participant(s): Yale University Bulldogs (0.500). No unique normalized participant match or unique kickoff pairing.
- `YSU` — market name(s): Youngstown St.; candidate participant(s): Youngstown St Penguins (0.500). No unique normalized participant match or unique kickoff pairing.

### `KXNCAAMBGAME`

- `HP` — market name(s): High Point; candidate participant(s): High Point Panthers (0.667). No unique normalized participant match or unique kickoff pairing.
- `LIB` — market name(s): Liberty; candidate participant(s): Liberty Flames (0.500). No unique normalized participant match or unique kickoff pairing.
- `ND` — market name(s): Notre Dame; candidate participant(s): Notre Dame Fighting Irish (0.500), Northern Kentucky Norse (0.250). No unique normalized participant match or unique kickoff pairing.
- `VILL` — market name(s): Villanova; candidate participant(s): Villanova Wildcats (0.500). No unique normalized participant match or unique kickoff pairing.

### `KXUELGAME`

- `CEL` — market name(s): Celtic, NK Celje; candidate participant(s): Celtic (1.000), NK Celje (1.000), NK Olimpija Ljubljana (0.250). Kalshi code identifies multiple Odds API participants.
- `LEV` — market name(s): Leverkusen, Levski Sofia; candidate participant(s): Bayer Leverkusen (1.000), PFC Levski Sofia (1.000), PFC CSKA Sofia (0.250). Kalshi code identifies multiple Odds API participants.

- Code collision: `CEL` is used for `Celtic` and `NK Celje`; it cannot be represented as a single sport-level alias.
- Code collision: `LEV` is used for `Bayer Leverkusen` and `PFC Levski Sofia`; it cannot be represented as a single sport-level alias.

### `KXUECLGAME`

- `AGF` — market name(s): Aarhus; candidate participant(s): AGF Aarhus (0.500). No unique normalized participant match or unique kickoff pairing.
- `ASM` — market name(s): Monaco; candidate participant(s): AS Monaco (0.500). No unique normalized participant match or unique kickoff pairing.
- `ATA` — market name(s): Atalanta; candidate participant(s): Atalanta BC (0.500). No unique normalized participant match or unique kickoff pairing.
- `BBL` — market name(s): Banja Luka; candidate participant(s): FK Borac Banja Luka (0.667). No unique normalized participant match or unique kickoff pairing.
- `BRI` — market name(s): Brighton; candidate participant(s): Brighton and Hove Albion (0.340). No unique normalized participant match or unique kickoff pairing.
- `CSK` — market name(s): CSKA Sofia; candidate participant(s): PFC CSKA Sofia (0.667), CSKA Moskva (0.333), PFC Levski Sofia (0.250). No unique normalized participant match or unique kickoff pairing.
- `CSU` — market name(s): Uni Craiova; candidate participant(s): Universitatea Craiova (0.333). No unique normalized participant match or unique kickoff pairing.
- `CZV` — market name(s): Crvena Zvezda; candidate participant(s): none. No unique normalized participant match or unique kickoff pairing.
- `EGR` — market name(s): Egnatia Rrogozhine; candidate participant(s): KF Egnatia (0.333). No unique normalized participant match or unique kickoff pairing.
- `ESC` — market name(s): IC Escaldes; candidate participant(s): Inter Club d'Escaldes (0.333). No unique normalized participant match or unique kickoff pairing.
- `HAJ` — market name(s): Hajduk; candidate participant(s): Hajduk Split (0.500). No unique normalized participant match or unique kickoff pairing.
- `HOM` — market name(s): Heart of Midlothian; candidate participant(s): Hearts (0.500). No unique normalized participant match or unique kickoff pairing.
- `IBE` — market name(s): Iberia; candidate participant(s): FC Iberia 1999 (0.500). No unique normalized participant match or unique kickoff pairing.
- `KAZ` — market name(s): Kauno; candidate participant(s): FK Kauno Žalgiris (0.500). No unique normalized participant match or unique kickoff pairing.
- `KUPS` — market name(s): Kuopion Palloseura; candidate participant(s): KuPS Kuopio (0.333). No unique normalized participant match or unique kickoff pairing.
- `MJA` — market name(s): Mjallby; candidate participant(s): Mjällby AIF (0.500). No unique normalized participant match or unique kickoff pairing.
- `SKB` — market name(s): Brann; candidate participant(s): SK Brann (0.500). No unique normalized participant match or unique kickoff pairing.
- `STT` — market name(s): St. Truidense; candidate participant(s): none. No unique normalized participant match or unique kickoff pairing.
- `TWE` — market name(s): Enschede; candidate participant(s): FC Twente Enschede (0.500). No unique normalized participant match or unique kickoff pairing.

## Participants without a confirmed current-market mapping

- `americanfootball_ncaaf`: 166 of 277 Odds API participants lack a confirmed mapping to a current Kalshi market code.
- `basketball_ncaab`: 386 of 386 Odds API participants lack a confirmed mapping to a current Kalshi market code.
- `soccer_uefa_europa_league`: 188 of 220 Odds API participants lack a confirmed mapping to a current Kalshi market code.
- `soccer_uefa_europa_conference_league`: 203 of 220 Odds API participants lack a confirmed mapping to a current Kalshi market code.

NCAAB has 386 participant records and four distinct current Kalshi team codes. The four market codes remain unresolved because the participant full names do not exactly match the normalized Kalshi winner names and there are no same-date Odds API events to confirm the pairs. The remaining 382 participants have no corresponding current market code; none were added to `teams.json`.

## Odds API credit accounting

The four `/events` requests were free (`x-requests-last=0`). The four `/participants` requests cost one credit each. Usage moved from 356 used / 144 remaining to 360 used / 140 remaining: 4 credits consumed, 140 remaining.
