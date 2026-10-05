# Generated persona set -- human review sheet

Generated 2026-09-14T20:41:39+00:00 by `harness/persona_gen.py`. Events window 2026-08-01 to 2026-09-13. Every event below was passed through the system's own `source_selection` judgment, re-read from its cited page, dated from that page, and judged by the system's own `materiality` call with the group description as the only context. **Candidate finding is NOT the system's query-formulation pipeline** except where `Found by` says `system_pipeline`: that pipeline was run five times on Premier League and yielded two verified events for about $2 (see the module docstring for the two causes, both findings about the system); the rest come from a directed web-search research call per group. `should_be_material` in the fixtures is the judge's verdict, not a human's -- fill the **Human verdict** column and correct the fixture where you disagree.

Run the set: `PYTHONPATH=src:. .venv/bin/python -m harness.run --fixture-dir harness/personas/generated --rounds 2`

Significance is derived from the judge's score (>=76 major, 40-75 borderline, <40 minor). The `learning` block on every fixture is schema v1: `prior_knowledge` (K) is the archetype's prior, clamped to its band and within 0.1 of |knows| / (|knows| + |does_not_know|); `memory_rate` (m) is a seeded uniform draw in [0.7, 1.4] independent of archetype, with four fast forgetters (m <= 0.8) and four slow forgetters (m >= 1.3) pinned across groups -- see the learning table in the summary.

## 1. Premier League (`premier_league`)

> English top-flight football: results, squads, injuries, refereeing and the transfer market.

### Events (5 verified, 3 judged material, 0 candidates rejected)  -- **short of 8**

| # | Date | Headline | Source | Found by | Judge | Judge's reason | Human verdict |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-04 | Falcons LB Jalon Walker tears ACL in training camp, out for 2026 season | [NFL.com](https://www.nfl.com/news/falcons-lb-jalon-walker-feared-to-have-torn-acl-in-training-camp-practice) | system_pipeline | material (72) | A first-round pick and productive young starter losing an entire season to a torn ACL in camp is exactly the kind of injury news NFL followers track, and it reshapes Atlanta's defensive outlook for 2026. It falls shor... |  |
| 2 | 2026-08-18 | Texans WR Jayden Higgins tears ACL, out for 2026 season | [NFL.com](https://www.nfl.com/news/texans-wr-jayden-higgins-torn-acl-out-2026-season) | system_pipeline | material (62) | A season-ending ACL tear to a recent second-round pick who was a productive rookie starter is significant NFL injury news, reported by a top insider during a heavily covered preseason window, and it reshapes Houston's... |  |
| 3 | 2026-09-01 | Summer 2026 Premier League transfer window closed on 1 September with a wave of deadline-day deals | [Sky Sports](https://www.skysports.com/transfer/news/12691/13579708/transfer-deadline-day-deals-summer-2026-confirmed-moves-across-premier-league-championship-efl-europe-and-more) | system_pipeline | material (92) | Deadline day itself is a fixture of the Premier League calendar and a £125m intra-league move of Enzo Fernandez from Chelsea to Manchester City would be the dominant talking point for weeks, shaping squad and title di... |  |
| 4 | 2026-09-11 | D'Andre Swift and the Bears agree to a three-year, $33.75 million contract extension | [Bleacher Report](https://bleacherreport.com/articles/25498360-dandre-swift-bears-reportedly-agree-contract-extension-ahead-2026-nfl-season) | system_pipeline | not material (3) | This is an NFL contract extension, entirely outside the domain of English top-flight football — no overlap with Premier League results, squads, injuries, refereeing or transfers. A Premier League follower would have n... |  |
| 5 | 2026-09-12 | Liverpool have four confirmed absentees and three doubts ahead of their Premier League match against Fulham. | [This Is Anfield](https://www.thisisanfield.com/2026/09/liverpool-team-news-vs-fulham-injuries-squad/) | system_pipeline | not material (32) | Routine pre-match team news: minor knocks and fitness doubts for one club ahead of a single fixture, with no long-term or high-profile blows confirmed. For a broad Premier League group, this level of granular injury r... |  |

### Subdomains (backfilled, one Sonnet call per group -- see the summary)

- `transfer market`: `transfer window`, `deadline-day`, `contract extension`, `extension`, `guaranteed`
- `injuries & fitness`: `hamstring injury`, `fitness test`, `adductor issue`, `cramp`, `adductor`, `hamstring`, `torn acl`, `acl`, `doubts`, `unavailable`
- `squad & matchday`: `squad`, `goalkeepers`
- `preseason & training`: `training camp`, `joint practice`
- `american football terms`: `overall pick`, `forced fumbles`, `fumble recovery`, `sacks`

### Personas

#### `06_amara_premier_league.json` -- Amara Okonkwo -- well_informed

- reading: `careful`; lowercase: false; noisy: no
- knows (10): `transfer window`, `deadline-day`, `hamstring injury`, `fitness test`, `adductor issue`, `cramp`, `doubts`, `unavailable`, `goalkeepers`, `squad`
- does_not_know (3): `torn acl`, `contract extension`, `overall pick`
- quantities: (none)
- learning: K=0.75, m=0.72 (**fast forgetter**)
- familiar_subdomains: `transfer market` 0.75, `injuries & fitness` 0.75, `squad & matchday` 0.85, `preseason & training` 0.75, `american football terms` 0.10 (K = vocabulary-weighted mean = 0.72)
- sample question (form `definition`): _Overall pick?_
- bio: Amara Okonkwo is a season-ticket holder who follows Premier League news obsessively, checking team news and injury updates every matchday.
- notes: Amara reads squad news and transfer coverage daily, so she knows the vocabulary around fitness doubts, injuries and deadline-day dealings inside out. She has no interest in American sports, so NFL-specific contract and injury terminology passes her by even when she skims a headline about it.

#### `07_tomasz_premier_league.json` -- Tomasz Wierzbicki -- partially_informed

- reading: `skimmer`; lowercase: true; noisy: yes -- silent_when_ignorant_rate=0.6, asks_about_known_rate=0.25
- knows (6): `torn acl`, `training camp`, `extension`, `transfer window`, `joint practice`, `guaranteed`
- does_not_know (6): `adductor`, `cramp`, `hamstring`, `forced fumbles`, `fumble recovery`, `fitness test`
- quantities: (none)
- learning: K=0.5, m=1.01
- familiar_subdomains: `transfer market` 0.60, `injuries & fitness` 0.45, `squad & matchday` 0.50, `preseason & training` 0.75, `american football terms` 0.25 (K = vocabulary-weighted mean = 0.50)
- sample question (form `extend`): _why does torn acl matter here?_
- bio: tomasz wierzbicki works in logistics in warsaw and follows the premier league mainly through highlight clips and transfer rumors on his phone.
- notes: he knows the shape of transfer deadlines and headline injury terms like torn acl because those dominate the sports app alerts he scrolls through, but he never reads the medical detail paragraphs so words like adductor or hamstring stay fuzzy to him. as the group's noisy persona he sometimes stays quiet when he hits a term he doesn't actually understand, and other times asks about something he already half-knows just to keep the chat going.

#### `08_priya_premier_league.json` -- Priya Raghunathan -- barely_informed

- reading: `bouncer`; lowercase: false; noisy: no
- knows (2): `transfer window`, `contract extension`
- does_not_know (12): `acl`, `forced fumbles`, `fumble recovery`, `sacks`, `guaranteed`, `deadline-day`, `cramp`, `adductor`, `hamstring injury`, `fitness test`, `doubts`, `squad`
- quantities: `sacks`
- learning: K=0.16, m=0.88
- familiar_subdomains: `transfer market` 0.35, `injuries & fitness` 0.10, `squad & matchday` 0.10, `preseason & training` 0.15, `american football terms` 0.05 (K = vocabulary-weighted mean = 0.16)
- sample question (form `definition`): _Sorry, what's fumble recovery in this?_
- bio: Priya catches Premier League highlights on her phone during her commute but rarely follows deeper football coverage.
- notes: Priya knows the basic rhythm of the season, like when the transfer window closes and when a player signs a new deal, because those show up in headlines she skims. She never reads injury reports closely, so medical terms and match-day squad details go over her head. She doesn't follow contract mechanics or stat breakdowns either, since those require reading past the headline.

#### `09_callum_premier_league.json` -- Callum Beattie -- disengaged

- reading: `bouncer`; lowercase: false; noisy: no
- knows (2): `torn acl`, `extension`
- does_not_know (10): `training camp`, `forced fumbles`, `fumble recovery`, `overall pick`, `transfer window`, `deadline-day`, `guaranteed`, `fitness test`, `adductor`, `hamstring`
- quantities: (none)
- learning: K=0.2, m=1.18
- familiar_subdomains: `transfer market` 0.30, `injuries & fitness` 0.30, `squad & matchday` 0.10, `preseason & training` 0.05, `american football terms` 0.05 (K = vocabulary-weighted mean = 0.22)
- sample question (form `-`): _(no turn -- configured to stay silent)_
- bio: Callum Beattie works night shifts at a warehouse and only catches sports headlines when they scroll past on his phone. He isn't tied to any particular club and mostly skims for injury news involving big names.
- notes: Callum picks up scraps of terminology from push notifications and headline scrolls rather than any deep following of a league. He recognizes an injury like a torn ACL or a contract extension because those words show up everywhere in sports coverage, but he has no grasp of the technical details behind training regimens, transfer dealings or specific injury types. His knowledge is shallow and event-driven, not built from sustained interest.

## 2. NFL (`nfl`)

> American football's NFL: results, injuries, trades, coaching changes and the start of the 2026 season.

### Events (8 verified, 6 judged material, 4 candidates rejected)

| # | Date | Headline | Source | Found by | Judge | Judge's reason | Human verdict |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-07 | Panthers beat Cardinals 33-30 in 2026 Hall of Fame Game in Canton, Ohio. | [NFL.com](https://www.nfl.com/news/panthers-cardinals-2026-hall-of-fame-game-what-we-learned-from-carolina-s-33-30-win) | directed_search | not material (33) | The Hall of Fame Game is a notable calendar marker (football is back) but its result is preseason exhibition noise — third-string QBs and a final score no one references a week later. A general NFL follower would know... |  |
| 2 | 2026-08-08 | Commanders LT Laremy Tunsil tears triceps in practice, could miss most of 2026 season | [CBS Sports](https://www.cbssports.com/nfl/news/commanders-laremy-tunsil-torn-triceps-will-miss-most-of-2026-season/) | directed_search | material (74) | A five-time Pro Bowl left tackle on a $60M extension suffering a season-ending triceps tear in training camp is a major injury story that dominates NFL news cycles and reshapes a team's outlook; it's the kind of item ... |  |
| 3 | 2026-08-14 | Cardinals rookie OL Chase Bisontis tore his MCL and will miss his entire rookie season. | [CBS Sports](https://www.cbssports.com/nfl/news/nfl-training-camp-injury-tracker/) | directed_search | not material (42) | A season-ending injury to a second-round interior lineman is real news for Cardinals fans, draft followers and fantasy/dynasty players, but offensive line rookies rarely register in general NFL conversation. No compet... |  |
| 4 | 2026-08-26 | NFL eliminates the Pro Bowl game, will name an 88-player Pro Bowl team instead | [NBC Sports (ProFootballTalk)](https://www.nbcsports.com/nfl/profootballtalk/rumor-mill/news/nfl-ditches-the-pro-bowl) | directed_search | material (80) | The permanent elimination of a 75-year-old NFL institution is a league-wide structural change that drew broad national coverage and sustained debate about all-star honors and player participation. For a general NFL-fo... |  |
| 5 | 2026-08-30 | NFL teams cut rosters to 53 players, releasing 1,184 players by Sunday 6 p.m. ET deadline | [CBS Sports](https://www.cbssports.com/nfl/news/2026-nfl-roster-cuts-tracker-live-updates-latest-moves-trade-rumors-as-all-32-teams-finalize-53-man-rosters/live/) | directed_search | material (72) | Final roster cutdown day is one of the biggest fixed points on the NFL calendar — over a thousand transactions in 48 hours, plus notable names like Will Levis being released — and it's the immediate precursor to Week ... |  |
| 6 | 2026-08-30 | Aaron Donald comes out of retirement to sign a one-year deal with the Rams for the 2026 season | [CBS Sports](https://www.cbssports.com/nfl/news/aaron-donald-unretires-returns-to-rams-for-2026-season/) | directed_search | material (92) | A first-ballot Hall of Fame talent and three-time DPOY unretiring days before the season opener, on a $20M deal joining another DPOY on the same defensive line, is exactly the kind of blockbuster that dominates NFL co... |  |
| 7 | 2026-09-09 | Seahawks beat Patriots 13-10 in NFL 2026 season opener rematch of Super Bowl LX | [Yahoo Sports](https://sports.yahoo.com/nfl/live/patriots-vs-seahawks-score-live-updates-highlights-stats-where-to-watch-week-1-season-opener-super-bowl-lx-rematch-222000691.html) | directed_search | material (87) | This is the NFL season opener and a Super Bowl LX rematch — the single most-watched game of the week — with a marquee storyline (the Super Bowl-winning team's starting QB injured on the fifth play, backup leads comeba... |  |
| 8 | 2026-09-10 | 49ers beat Rams 27-7 in NFL's first regular-season game in Australia | [49ers.com](https://www.49ers.com/news/49ers-open-2026-with-27-7-win-over-rams-in-melbourne-takeaways-from-larvssf) | directed_search | material (84) | This is a season-opening divisional game and the NFL's first-ever regular-season game in Australia — a historic league milestone that would dominate NFL conversation, plus a lopsided 49ers win and Fred Warner breaking... |  |

<details><summary>Rejected candidates</summary>

- Seahawks beat Patriots 29-13 in 2026 Kickoff Game -- page does not support the event: The page is a pre-game preview article listing storylines to watch for Wednesday's kickoff game; the 29-13 score referenced is from the prior Super Bowl LX matchup, not a reported result of the 2026 season opener, so the candidate's claim about the kickoff game result is not supported by this page. -- <https://www.nfl.com/news/patriots-vs-seahawks-five-must-know-storylines-for-wednesday-s-2026-nfl-kickoff-game>
- Seahawks rookie safety Bud Clark breaks ankle in preseason -- duplicate URL -- <https://www.cbssports.com/nfl/news/nfl-training-camp-injury-tracker/>
- Panthers acquire DT TeRah Edwards from the Chargers -- verification incomplete after retry: occurred_at must be YYYY-MM-DD; none was established -- <https://www.espn.com/nfl/story/_/id/48915793/2026-nfl-offseason-trade-tracker-updates-news-contracts-training-camp-preseason>
- Myles Garrett to have knee surgery and go on injured reserve -- page unreadable (thin or non-2xx body) and search fallback found no copy -- <https://www.espn.com/nfl/story/_/id/49928065/sources-rams-myles-garrett-knee-surgery-headed-ir>

</details>

### Subdomains (backfilled, one Sonnet call per group -- see the summary)

- `on field play & stats`: `touchdown`, `interceptions`, `quarterback`, `bull rush`, `tackles`, `tackles record`, `practice`, `preseason opener`, `regular season`
- `injuries & medical`: `torn triceps`, `mcl`, `hip injury`, `triceps`
- `roster & contract mechanics`: `waiver wire`, `roster cuts`, `extension`, `incentives`, `rosters`
- `draft & awards`: `overall pick`, `draft class`, `pro bowl`
- `alternative formats`: `flag football`

### Personas

#### `10_yuki_nfl.json` -- Yuki Tanabe -- well_informed

- reading: `careful`; lowercase: true; noisy: no
- knows (12): `waiver wire`, `roster cuts`, `torn triceps`, `mcl`, `interceptions`, `touchdown`, `extension`, `incentives`, `tackles record`, `hip injury`, `bull rush`, `overall pick`
- does_not_know (3): `pro bowl`, `flag football`, `draft class`
- quantities: (none)
- learning: K=0.79, m=1.26
- familiar_subdomains: `on field play & stats` 0.90, `injuries & medical` 0.85, `roster & contract mechanics` 0.88, `draft & awards` 0.50, `alternative formats` 0.75 (K = vocabulary-weighted mean = 0.79)
- sample question (form `check_belief`): _is touchdown the same thing here, or am i mixing it up? i only skimmed it_
- bio: yuki tanabe is a longtime nfl fan who tracks roster moves and injury reports for several fantasy leagues. she reads beat-writer recaps every morning during the season.
- notes: yuki follows day-to-day nfl transaction news closely, so she knows the vocabulary around injuries, contracts, and roster mechanics well. she pays less attention to award-format changes and draft-specific terminology since those come up only a few times a year. her gaps show up around structural or ceremonial league changes rather than on-field or transactional language.

#### `11_rosa_nfl.json` -- Rosa Delgado -- partially_informed

- reading: `skimmer`; lowercase: false; noisy: no
- knows (5): `touchdown`, `quarterback`, `preseason opener`, `roster cuts`, `practice`
- does_not_know (5): `torn triceps`, `mcl`, `waiver wire`, `tackles record`, `flag football`
- quantities: (none)
- learning: K=0.49, m=1.36 (**slow forgetter**)
- familiar_subdomains: `on field play & stats` 0.65, `injuries & medical` 0.25, `roster & contract mechanics` 0.50, `draft & awards` 0.30, `alternative formats` 0.20 (K = vocabulary-weighted mean = 0.49)
- sample question (form `check_belief`): _So does that mean quarterback is affected too?_
- bio: Rosa Delgado works in retail management and watches NFL games most Sundays with her family, following her hometown team closely.
- notes: Rosa knows the on-field vocabulary from watching games and highlights every week, so touchdowns, quarterbacks, and roster cuts are familiar. She doesn't follow the medical or transactional side of the league closely, so injury specifics and formal league procedures pass her by. Her knowledge comes from casual weekend viewing rather than reading beat reporters or injury reports.

#### `12_kwame_nfl.json` -- Kwame Mensah -- barely_informed

- reading: `bouncer`; lowercase: true; noisy: no
- knows (2): `touchdown`, `preseason opener`
- does_not_know (10): `torn triceps`, `mcl`, `waiver wire`, `pro bowl`, `incentives`, `hip injury`, `interceptions`, `extension`, `rosters`, `tackles`
- quantities: (none)
- learning: K=0.17, m=0.72 (**fast forgetter**)
- familiar_subdomains: `on field play & stats` 0.30, `injuries & medical` 0.10, `roster & contract mechanics` 0.10, `draft & awards` 0.10, `alternative formats` 0.10 (K = vocabulary-weighted mean = 0.17)
- sample question (form `extend`): _more on the touchdown part?_
- bio: kwame catches nfl highlights on his phone most sundays but rarely follows the league beyond that.
- notes: kwame knows the basic on-field terms because he watches game recaps and scores, but he doesn't follow roster management, injury specifics, or league business news closely. he tunes out anything involving contracts, medical details, or offseason procedures. his football knowledge is limited to what shows up in a quick highlight reel.

#### `13_ingrid_nfl.json` -- Ingrid Solheim -- disengaged

- reading: `bouncer`; lowercase: false; noisy: yes -- never_engages=True
- knows (2): `touchdown`, `quarterback`
- does_not_know (10): `triceps`, `mcl`, `waiver wire`, `pro bowl`, `extension`, `interceptions`, `tackles record`, `incentives`, `regular season`, `roster cuts`
- quantities: (none)
- learning: K=0.18, m=1.29
- familiar_subdomains: `on field play & stats` 0.30, `injuries & medical` 0.10, `roster & contract mechanics` 0.10, `draft & awards` 0.10, `alternative formats` 0.10 (K = vocabulary-weighted mean = 0.18)
- sample question (form `-`): _(no turn -- configured to stay silent)_
- bio: Ingrid Solheim works long shifts as a hospital scheduler and treats the TV in the break room as background noise. She glances at NFL headlines only when a coworker brings them up.
- notes: She knows the absolute basics of the sport, like what a touchdown is and that a quarterback throws the ball, from years of half-watching games with family. She never says anything, only reads, per the noise rule. Beyond that she tunes out anything about injuries, roster mechanics, or league business since none of it sticks without regular viewing. Contract, medical, and structural terms pass right by her.

## 3. Formula 1 (`formula_1`)

> F1 grands prix, driver and constructor standings, technical regulations, team and driver moves.

_No events file -- stage A did not reach this group._

## 4. Startups and VC (`startups_vc`)

> Funding rounds, exits and market conditions in venture-backed technology companies.

_No events file -- stage A did not reach this group._

## 5. Crypto (`crypto`)

> Bitcoin, Ethereum and major tokens: prices, ETF flows, exchange and protocol events, regulation.

### Events (8 verified, 7 judged material, 0 candidates rejected)

| # | Date | Headline | Source | Found by | Judge | Judge's reason | Human verdict |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-03 | Coldcard hardware wallet flaw led to theft of ~1,816 BTC (~$116 million) from over 5,200 addresses | [Fortune](https://fortune.com/2026/08/03/bitcoin-owners-116-million-hack-coldcard-coinkite-exploit/) | directed_search | material (86) | A ~$116M theft exploiting a flaw in Coldcard, one of the best-known Bitcoin hardware wallets, hits the core self-custody narrative and would dominate crypto conversation for days; the vendor's urgent 'move your funds'... |  |
| 2 | 2026-08-18 | SEC proposes 'Regulation Crypto Assets' to create a tailored offering framework for crypto assets, opening a 60-day comment period. | [U.S. Securities and Exchange Commission](https://www.sec.gov/newsroom/press-releases/2026-76-sec-proposes-new-regulation-crypto-assets) | directed_search | material (89) | A formal SEC proposed rule creating a dedicated registration exemption and safe harbor for crypto token offerings — plus state-law preemption — is one of the most consequential regulatory developments the crypto group... |  |
| 3 | 2026-08-24 | Coinbase launches tokenized U.S. stocks on Base, starting with Apple, Nvidia, Meta and Alphabet | [CoinDesk](https://www.coindesk.com/business/2026/08/24/coinbase-debuts-tokenized-stocks-on-base-network-joining-race-to-bring-equities-on-blockchain) | directed_search | material (76) | Coinbase — the largest US exchange — bringing tokenized megacap equities onto Base is a headline event in the RWA/tokenization narrative that dominates crypto discourse, and it directly affects Base activity, Chainlin... |  |
| 4 | 2026-08-27 | Bitcoin climbed back above $80,000 on August 27 as U.S. spot bitcoin ETFs logged an eighth straight day of net inflows. | [CoinDesk](https://www.coindesk.com/business/2026/08/27/live-updates-bitcoin-etf-inflows-hit-eight-straight-days-as-august-tops-usd3-billion) | directed_search | material (57) | Price level and spot-ETF flow data are the core running conversation for this group, and an eight-session inflow streak totaling ~$2.8B plus reclaiming a round $80K level is the kind of thing anyone conversant would h... |  |
| 5 | 2026-08-31 | US spot Bitcoin ETFs post $3.52 billion in net inflows in August 2026 as Bitcoin gains about 25% | [Cointelegraph](https://cointelegraph.com/markets/bitcoin-etf-best-month-2026-btc-up-25-august) | directed_search | material (86) | A ~25% monthly Bitcoin rally plus the year's largest spot-ETF inflow month is headline-level market news that dominates crypto conversation; ETF flows are explicitly core to this group's scope. No saturating prior eve... |  |
| 6 | 2026-08-31 | Strategy resumes Bitcoin buying with 4,603 BTC purchase for $369.7 million | [TheStreet (via Yahoo Finance)](https://finance.yahoo.com/markets/crypto/articles/microstrategy-finally-buys-bitcoin-2-151313122.html) | directed_search | material (58) | Strategy's BTC purchases are a recurring, near-routine headline in crypto, which caps materiality, but this one breaks a two-month pause and involves a large equity raise plus preferred-stock buyback — a thread crypto... |  |
| 7 | 2026-08-31 | Spot Ethereum ETFs in the US drew $1.75 billion in August 2026, their best month since August 2025. | [Crypto Briefing](https://cryptobriefing.com/spot-eth-etf-inflows-august-2026/) | directed_search | material (62) | ETF flows are explicitly core to this group's scope, and a $1.75B monthly inflow reversing prior outflows and marking a 12-month high is the kind of headline number crypto followers quote in conversation. It's still a... |  |
| 8 | 2026-09-02 | Bitcoin and Ethereum prices fell 1.5% and 2.0% respectively on September 2, 2026 amid renewed US-Iran conflict. | [Yahoo Finance](https://finance.yahoo.com/personal-finance/investing/article/bitcoin-and-ethereum-prices-today-wednesday-september-2-2026-crypto-prices-tumble-as-iran-war-reignites-112639522.html) | directed_search | not material (28) | A 1.5–2.0% single-day move in BTC/ETH is well within normal daily volatility and is the kind of routine price-recap article that crypto followers ignore; not knowing it costs nothing socially. The geopolitical driver ... |  |

### Subdomains (backfilled, one Sonnet call per group -- see the summary)

- `self custody & wallets`: `hardware wallet`, `seed phrases`, `self-custody`, `wallet`, `recovery-phrase`
- `etf & market flows`: `spot bitcoin etfs`, `net inflows`, `price feeds`, `inflow streak`, `ethereum`, `bitcoin treasury`, `price`, `stock`
- `regulation & securities law`: `safe harbor`, `offering regime`, `public comment period`, `securities act`, `comment period`, `exemptions`
- `corporate treasury & equities`: `treasury company`, `common shares`, `average acquisition price`, `treasury`, `treasury stocks`, `tokenized`
- `macro & commodities`: `oil prices`

### Personas

#### `22_farida_crypto.json` -- Farida Nazarova -- well_informed

- reading: `careful`; lowercase: false; noisy: no
- knows (11): `hardware wallet`, `seed phrases`, `spot bitcoin etfs`, `net inflows`, `safe harbor`, `tokenized`, `self-custody`, `price feeds`, `treasury company`, `common shares`, `inflow streak`
- does_not_know (3): `offering regime`, `public comment period`, `average acquisition price`
- quantities: `average acquisition price`, `net inflows`
- learning: K=0.79, m=1.36 (**slow forgetter**)
- familiar_subdomains: `self custody & wallets` 0.85, `etf & market flows` 0.90, `regulation & securities law` 0.65, `corporate treasury & equities` 0.75, `macro & commodities` 0.75 (K = vocabulary-weighted mean = 0.79)
- sample question (form `extend`): _How does this affect seed phrases?_
- bio: Farida trades crypto part-time and has followed Bitcoin and Ethereum markets closely for several years. She reads CoinDesk and Cointelegraph daily and keeps a spreadsheet tracking her own portfolio against ETF flow data.
- notes: Farida tracks ETF inflow numbers, treasury company balance sheets, and self-custody practices closely because she manages her own wallet and follows market-structure news obsessively. She's less familiar with the regulatory machinery behind new SEC rulemaking, since she reads headlines about proposals but doesn't dig into the procedural language of exemptions or comment periods.

#### `23_malik_crypto.json` -- Malik Thompson -- partially_informed

- reading: `skimmer`; lowercase: true; noisy: no
- knows (5): `ethereum`, `net inflows`, `spot bitcoin etfs`, `bitcoin treasury`, `oil prices`
- does_not_know (5): `seed phrases`, `safe harbor`, `self-custody`, `tokenized`, `price feeds`
- quantities: (none)
- learning: K=0.46, m=0.81
- familiar_subdomains: `self custody & wallets` 0.25, `etf & market flows` 0.60, `regulation & securities law` 0.30, `corporate treasury & equities` 0.30, `macro & commodities` 0.50 (K = vocabulary-weighted mean = 0.46)
- sample question (form `definition`): _what is seed phrases?_
- bio: malik thompson works in logistics and checks crypto prices on his phone most mornings, mostly following bitcoin and a few big-name tokens.
- notes: malik follows price action and etf headlines closely because he holds a bit of bitcoin and ethereum himself, so terms like net inflows and spot bitcoin etfs are familiar from his daily scrolling. he doesn't follow the more technical or regulatory side of the space, so wallet security details and securities-law mechanics like safe harbor or seed phrases go over his head. he's picked up broader market-linked phrases like oil prices from headlines about crypto reacting to global events. he's never used a hardware wallet or looked into tokenization projects, so those stay fuzzy for him.

#### `24_hana_crypto.json` -- Hana Kovacs -- barely_informed

- reading: `bouncer`; lowercase: false; noisy: no
- knows (2): `wallet`, `stock`
- does_not_know (10): `seed phrases`, `safe harbor`, `securities act`, `net inflows`, `tokenized`, `self-custody`, `price feeds`, `treasury`, `comment period`, `recovery-phrase`
- quantities: (none)
- learning: K=0.2, m=1.08
- familiar_subdomains: `self custody & wallets` 0.30, `etf & market flows` 0.30, `regulation & securities law` 0.10, `corporate treasury & equities` 0.10, `macro & commodities` 0.15 (K = vocabulary-weighted mean = 0.22)
- sample question (form `definition`): _What does recovery-phrase mean here? for context I'm pretty new to this._
- bio: Hana Kovacs works in logistics coordination and glances at crypto headlines mostly when her brother texts her about bitcoin prices.
- notes: Hana only knows crypto as 'digital money in a wallet' and occasionally checks if a stock she half-remembers is up or down. She has no grasp of how ETFs, custody, or regulatory filings actually work, and terms like tokenization or securities law are just noise to her. Her understanding comes from skimmed push notifications, not sustained reading.

#### `25_bruno_crypto.json` -- Bruno Ferreira -- disengaged

- reading: `bouncer`; lowercase: false; noisy: yes -- never_engages=True
- knows (2): `hardware wallet`, `price`
- does_not_know (10): `safe harbor`, `spot bitcoin etfs`, `net inflows`, `tokenized`, `self-custody`, `seed phrases`, `recovery-phrase`, `treasury stocks`, `comment period`, `exemptions`
- quantities: (none)
- learning: K=0.2, m=0.8 (**fast forgetter**)
- familiar_subdomains: `self custody & wallets` 0.30, `etf & market flows` 0.30, `regulation & securities law` 0.10, `corporate treasury & equities` 0.10, `macro & commodities` 0.10 (K = vocabulary-weighted mean = 0.22)
- sample question (form `-`): _(no turn -- configured to stay silent)_
- bio: Bruno Ferreira works in logistics and glances at financial headlines mostly out of habit. He owns a small amount of Bitcoin bought years ago but doesn't follow the market closely.
- notes: Bruno recognizes the basic idea of a wallet and knows that things have a price, but the deeper mechanics of crypto markets pass him by. He's never engaged with ETF structures, regulatory filings, or token custody schemes, so terms like safe harbor exemptions or self-custody mean little to him. This persona reads the briefings but never responds in the conversation.

## 6. AI industry (`ai_trends`)

> Model releases, AI company funding and deals, compute and chips, AI policy.

### Events (8 verified, 6 judged material, 2 candidates rejected)

| # | Date | Headline | Source | Found by | Judge | Judge's reason | Human verdict |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-06 | Microsoft's India South Central cloud region in Hyderabad becomes generally available | [Microsoft Source Asia](https://news.microsoft.com/source/asia/features/microsofts-newest-india-datacenter-region-goes-live-to-power-the-countrys-ai-economy-and-enable-frontier-firms/) | directed_search | not material (32) | A new Azure region going GA is routine cloud infrastructure news; the AI-industry-relevant angle is the $20.5B India commitment, which was itself announced earlier and is the more memorable item. For a group focused o... |  |
| 2 | 2026-08-12 | Koray Kavukcuoglu named SVP to lead Google DeepMind's frontier AI development, replacing Demis Hassabis in that role | [CNBC](https://www.cnbc.com/2026/08/12/google-deepmind-koray-kavukcuoglu.html) | directed_search | material (86) | A leadership change at the top of Google DeepMind — with Hassabis stepping aside from running frontier model development into a chair role and Kavukcuoglu reporting directly to Pichai — is exactly the kind of headline... |  |
| 3 | 2026-08-13 | Anthropic to embed machine-readable watermarks in Claude AI outputs from August 2, 2026 to comply with EU AI Act | [Artificial Lawyer](https://www.artificiallawyer.com/2026/08/13/anthropic-will-embed-watermarks-in-ai-outputs/) | directed_search | material (68) | A frontier lab committing to machine-readable watermarking across all outputs — and doing it globally rather than EU-only — is a first-of-its-kind compliance move under the AI Act's Article 50, sitting squarely in thi... |  |
| 4 | 2026-08-16 | Anthropic's Claude suffers major outage affecting Claude.ai, Claude Code and Claude Cowork. | [Bleeping Computer](https://www.bleepingcomputer.com/news/artificial-intelligence/anthropic-confirms-claude-is-down-in-major-outage-affecting-multiple-services/) | directed_search | not material (27) | A ~45-minute authentication outage that was fully resolved the same hour is operational noise rather than an industry-shaping event; the group's stated focus is model releases, funding, compute, and policy. It generat... |  |
| 5 | 2026-08-26 | Amazon and Nvidia expand AWS partnership with 2 million additional Nvidia GPUs for 2027-2028 | [TechCrunch](https://techcrunch.com/2026/08/26/amazon-just-tripled-its-order-of-nvidia-chips-over-surging-demand/) | directed_search | material (84) | A hyperscaler tripling its Nvidia order to ~2M GPUs spanning Blackwell Ultra through Rubin Ultra is a headline compute/chips datapoint that speaks directly to AI demand, capex, and Nvidia's roadmap — core subject matt... |  |
| 6 | 2026-09-01 | US urges deregulation while EU enforces AI Act at G20 innovation ministerial in Chapel Hill | [Al Jazeera](https://www.aljazeera.com/news/2026/9/2/us-pushes-looser-approach-to-ai-regulation-while-eu-pushes-new-law) | directed_search | material (74) | A high-profile transatlantic regulatory clash — US officials pushing 'Carolina Principles' deregulation with Zuckerberg and Musk present, while the Commission simultaneously opens preliminary AI Act information reques... |  |
| 7 | 2026-09-02 | Cognition AI is set to raise about $1 billion at a $47 billion valuation. | [Bloomberg](https://www.bloomberg.com/news/articles/2026-09-02/ai-startup-cognition-set-to-raise-around-1-billion-at-a-47-billion-value) | directed_search | material (81) | A ~$1B raise at a $47B valuation for a prominent AI coding startup (with ~$10B in reported investor demand) is a headline-tier funding event in a group explicitly defined around AI company funding and deals, and it im... |  |
| 8 | 2026-09-02 | Meta agrees to $18 billion youth-safety settlement with 29 state attorneys general | [CNBC](https://www.cnbc.com/2026/09/02/meta-18-billion-settlement-ai-products.html) | directed_search | material (60) | An $18B settlement by Meta is a headline-dominating tech story that anyone following the AI industry would almost certainly have seen, and it touches AI-adjacent policy (design features, age verification, teen safety)... |  |

<details><summary>Rejected candidates</summary>

- Nvidia says Rubin platform is in full production -- event dated 2026-01-05, outside 2026-08-01..2026-09-13 -- <https://nvidianews.nvidia.com/news/rubin-platform-ai-supercomputer>
- Microsoft plans to more than triple data-center capacity to over 38 gigawatts by 2032 -- page unreadable (HTTP 403) and search fallback found no copy -- <https://www.bloomberg.com/news/features/2026-09-10/microsoft-ai-focused-data-center-plan-to-add-26-gigawatts-of-compute>

</details>

### Subdomains (backfilled, one Sonnet call per group -- see the summary)

- `compute & infrastructure`: `cloud region`, `availability zones`, `gpus`, `blackwell ultra`, `networking hardware`, `outage`
- `frontier models & safety`: `frontier ai`, `watermark`, `machine-readable`, `robotics platform`
- `funding & market`: `valuation`, `funding round`, `settlement`
- `policy & compliance`: `eu ai act`, `age verification`, `usage limit`, `legal charge`, `authentication issues`, `information requests`, `authentication`

### Personas

#### `26_sipho_ai_trends.json` -- Sipho Dlamini -- well_informed

- reading: `skimmer`; lowercase: true; noisy: no
- knows (12): `cloud region`, `availability zones`, `gpus`, `blackwell ultra`, `frontier ai`, `watermark`, `machine-readable`, `valuation`, `funding round`, `legal charge`, `age verification`, `usage limit`
- does_not_know (4): `robotics platform`, `networking hardware`, `authentication issues`, `information requests`
- quantities: `legal charge`, `usage limit`, `valuation`
- learning: K=0.75, m=0.87
- familiar_subdomains: `compute & infrastructure` 0.72, `frontier models & safety` 0.75, `funding & market` 0.85, `policy & compliance` 0.55 (K = vocabulary-weighted mean = 0.69)
- sample question (form `extend`): _can you say more about the cloud region side of it?_
- bio: works as a cloud infrastructure engineer in Johannesburg and follows AI industry news obsessively before his shift starts.
- notes: sipho tracks model releases, compute deals and policy fights closely because they affect his own infrastructure work, so he's fluent in terms like gpus and cloud regions. he skims past the internal engineering details of outages and hardware integration that don't touch his own stack, which is why authentication issues and networking hardware slip past him. he mostly reads headlines and skips the fine print on cross-company technical integrations.

#### `27_elena_ai_trends.json` -- Elena Marchetti -- partially_informed

- reading: `bouncer`; lowercase: false; noisy: no
- knows (6): `cloud region`, `gpus`, `outage`, `funding round`, `valuation`, `settlement`
- does_not_know (5): `eu ai act`, `machine-readable`, `availability zones`, `legal charge`, `age verification`
- quantities: `legal charge`, `valuation`
- learning: K=0.52, m=1.29
- familiar_subdomains: `compute & infrastructure` 0.60, `frontier models & safety` 0.30, `funding & market` 0.75, `policy & compliance` 0.25 (K = vocabulary-weighted mean = 0.52)
- sample question (form `bridge`): _What's availability zones -- is that related to cloud region or separate?_
- bio: Elena Marchetti works in product marketing at a mid-size fintech and follows AI industry news mostly through newsletters and social feeds. She's curious about the space but doesn't work directly with the technology.
- notes: Elena picks up the big-picture business vocabulary from headlines about deals, valuations, and outages because those stories get wide coverage and plain-language explainers. She hasn't absorbed the regulatory or infrastructure jargon that shows up mainly in specialist reporting, like specific compliance clauses or hardware naming conventions. Her understanding is shaped by what trends on business news aggregators rather than technical deep-dives.

#### `28_tariq_ai_trends.json` -- Tariq Rahman -- barely_informed

- reading: `careful`; lowercase: true; noisy: no
- knows (3): `outage`, `cloud region`, `settlement`
- does_not_know (12): `frontier ai`, `machine-readable`, `watermark`, `eu ai act`, `gpus`, `availability zones`, `legal charge`, `age verification`, `networking hardware`, `robotics platform`, `valuation`, `usage limit`
- quantities: `settlement`, `usage limit`, `valuation`
- learning: K=0.19, m=0.73 (**fast forgetter**)
- familiar_subdomains: `compute & infrastructure` 0.30, `frontier models & safety` 0.10, `funding & market` 0.30, `policy & compliance` 0.10 (K = vocabulary-weighted mean = 0.19)
- sample question (form `bridge`): _what is availability zones, is it a kind of cloud region? asking because i only half follow this_
- bio: Tariq works in retail management and skims tech headlines on his phone during breaks. He doesn't follow AI news closely but notices when it affects services he uses.
- notes: Tariq picks up on everyday outcomes he can picture, like a service going down or a company paying a big settlement, because those show up in normal news summaries. He has no exposure to the technical or regulatory vocabulary behind AI infrastructure, chips, or compliance rules, since he never reads past headlines on those topics.

#### `29_chloe_ai_trends.json` -- Chloe Whitfield -- disengaged

- reading: `bouncer`; lowercase: false; noisy: no
- knows (3): `outage`, `cloud region`, `settlement`
- does_not_know (11): `availability zones`, `watermark`, `eu ai act`, `gpus`, `valuation`, `frontier ai`, `authentication`, `age verification`, `legal charge`, `usage limit`, `networking hardware`
- quantities: `legal charge`, `usage limit`, `valuation`
- learning: K=0.19, m=1.37 (**slow forgetter**)
- familiar_subdomains: `compute & infrastructure` 0.30, `frontier models & safety` 0.08, `funding & market` 0.30, `policy & compliance` 0.07 (K = vocabulary-weighted mean = 0.19)
- sample question (form `-`): _(no turn -- configured to stay silent)_
- bio: Chloe Whitfield works in retail operations and mostly follows sports news, only glancing at tech headlines when they trend.
- notes: Chloe skims tech news mainly through push notifications and social media summaries, so she recognizes only the most everyday words like outage or settlement. She doesn't follow the technical or regulatory side of AI companies closely enough to pick up terms like watermarking, GPUs, or the EU AI Act. Her attention is brief and she rarely digs past a headline.

## 7. Stocks and markets (`public_markets`)

> US equities, major earnings, the Fed and rates, notable single-stock moves.

_No events file -- stage A did not reach this group._

## 8. Wine trade (`wine_trade`)

> Harvest, vintage conditions, appellation rules and the commercial state of the wine industry.

_No events file -- stage A did not reach this group._

## 9. Film and TV (`film_tv`)

> Box office, streaming, awards season, major releases and industry deals.

_No events file -- stage A did not reach this group._

## 10. Music (`music`)

> Charts, tours, releases, label and streaming business, awards.

_No events file -- stage A did not reach this group._

## Summary

- Groups: 10; events files present: 4; verified events: 29.

| Group | Events | Material | Rejected | Short of 8 |
|---|---|---|---|---|
| `premier_league` | 5 | 3 | 0 | **yes** |
| `nfl` | 8 | 6 | 4 | no |
| `formula_1` | 0 | 0 | 0 | **yes** |
| `startups_vc` | 0 | 0 | 0 | **yes** |
| `crypto` | 8 | 7 | 0 | no |
| `ai_trends` | 8 | 6 | 2 | no |
| `public_markets` | 0 | 0 | 0 | **yes** |
| `wine_trade` | 0 | 0 | 0 | **yes** |
| `film_tv` | 0 | 0 | 0 | **yes** |
| `music` | 0 | 0 | 0 | **yes** |

- Groups short of 8 events: `premier_league`, `formula_1`, `startups_vc`, `public_markets`, `wine_trade`, `film_tv`, `music`.
- Personas written: 16 (barely_informed 4, disengaged 4, partially_informed 4, well_informed 4).
- Noisy personas (3): `tomasz_premier_league`, `ingrid_nfl`, `bruno_crypto`.
- Personas that failed validation: none.

### Learning traits (schema v1)

- Fast forgetters (m <= 0.8), 4: `06_amara_premier_league.json` (m=0.72), `12_kwame_nfl.json` (m=0.72), `25_bruno_crypto.json` (m=0.8), `28_tariq_ai_trends.json` (m=0.73).
- Slow forgetters (m >= 1.3), 3: `11_rosa_nfl.json` (m=1.36), `22_farida_crypto.json` (m=1.36), `29_chloe_ai_trends.json` (m=1.37).

| File | Persona | Archetype | Group | K (prior_knowledge) | m (memory_rate) | |
|---|---|---|---|---|---|---|
| `06_amara_premier_league.json` | Amara Okonkwo | well_informed | `premier_league` | 0.75 | 0.72 | fast forgetter |
| `07_tomasz_premier_league.json` | Tomasz Wierzbicki | partially_informed | `premier_league` | 0.5 | 1.01 |  |
| `08_priya_premier_league.json` | Priya Raghunathan | barely_informed | `premier_league` | 0.16 | 0.88 |  |
| `09_callum_premier_league.json` | Callum Beattie | disengaged | `premier_league` | 0.2 | 1.18 |  |
| `10_yuki_nfl.json` | Yuki Tanabe | well_informed | `nfl` | 0.79 | 1.26 |  |
| `11_rosa_nfl.json` | Rosa Delgado | partially_informed | `nfl` | 0.49 | 1.36 | slow forgetter |
| `12_kwame_nfl.json` | Kwame Mensah | barely_informed | `nfl` | 0.17 | 0.72 | fast forgetter |
| `13_ingrid_nfl.json` | Ingrid Solheim | disengaged | `nfl` | 0.18 | 1.29 |  |
| `22_farida_crypto.json` | Farida Nazarova | well_informed | `crypto` | 0.79 | 1.36 | slow forgetter |
| `23_malik_crypto.json` | Malik Thompson | partially_informed | `crypto` | 0.46 | 0.81 |  |
| `24_hana_crypto.json` | Hana Kovacs | barely_informed | `crypto` | 0.2 | 1.08 |  |
| `25_bruno_crypto.json` | Bruno Ferreira | disengaged | `crypto` | 0.2 | 0.8 | fast forgetter |
| `26_sipho_ai_trends.json` | Sipho Dlamini | well_informed | `ai_trends` | 0.75 | 0.87 |  |
| `27_elena_ai_trends.json` | Elena Marchetti | partially_informed | `ai_trends` | 0.52 | 1.29 |  |
| `28_tariq_ai_trends.json` | Tariq Rahman | barely_informed | `ai_trends` | 0.19 | 0.73 | fast forgetter |
| `29_chloe_ai_trends.json` | Chloe Whitfield | disengaged | `ai_trends` | 0.19 | 1.37 | slow forgetter |

### Subdomain backfill (stage C)

`group.subdomains` / `familiar_subdomains` were added to fixtures already on disk by `python -m harness.persona_gen subdomains`: ONE `claude-sonnet-5` call per group (retried once on a rule violation) proposing the taxonomy over the union of the group's four vocabularies and a weight per persona from the existing knows / does_not_know, bio and notes. No term-matching heuristic was used. `prior_knowledge` was then recomputed as the vocabulary-weighted mean of the weights (clamped to the archetype band); changes are listed. The calls are in the cost ledger under stage C.

| Group | Status | Calls | Run | Subdomains | K changes |
|---|---|---|---|---|---|
| `ai_trends` | ok | 2 | `persona_gen_20260914T203550Z` | `compute & infrastructure`, `frontier models & safety`, `funding & market`, `policy & compliance` | none |
| `crypto` | ok | 2 | `persona_gen_20260914T203550Z` | `self custody & wallets`, `etf & market flows`, `regulation & securities law`, `corporate treasury & equities`, `macro & commodities` | malik_crypto K 0.5 -> 0.46; hana_crypto K 0.17 -> 0.2; bruno_crypto K 0.17 -> 0.2 |
| `nfl` | ok | 2 | `persona_gen_20260914T203550Z` | `on field play & stats`, `injuries & medical`, `roster & contract mechanics`, `draft & awards`, `alternative formats` | yuki_nfl K 0.8 -> 0.79; rosa_nfl K 0.5 -> 0.49; ingrid_nfl K 0.17 -> 0.18 |
| `premier_league` | ok | 2 | `persona_gen_20260914T203550Z` | `transfer market`, `injuries & fitness`, `squad & matchday`, `preseason & training`, `american football terms` | none |

- `ai_trends`: The vocabulary splits naturally into hardware/infrastructure terms, model-and-safety-technology terms, business/deal terms, and regulatory/legal terms, mirroring standard AI industry coverage beats. Sipho's frontier models & safety weight was lowered from 0.88 to 0.75 so it stays below 0.8 (per his do_not_know term 'robotics platform') while remaining above 0.7 to satisfy the well_informed band and keep policy & compliance as his single weaker subdomain. All other reader weights were left as they already satisfied the knows/does_not_know threshold rules for their archetypes.
- `crypto`: The crypto glossary splits naturally into personal custody practices, market/ETF flow terminology, securities-law/regulatory procedure, and corporate treasury/equity mechanics, with a small leftover bucket for the off-topic 'oil prices' term. Farida's regulation and treasury weights were raised above the 0.6 'weaker' threshold (while staying below 0.80 to respect her does_not_know terms) so that, as a well_informed reader, she has no more than one weak subdomain, and all other readers' weights were left mapping their knows/does_not_know terms into the required bands.
- `nfl`: The glossary splits naturally into what happens during games (on-field play & stats), player health (injuries & medical), front-office/contract mechanics (roster & contract mechanics), draft/award ceremony terms (draft & awards), and a leftover bucket for the off-topic 'flag football' term (alternative formats). Weights were set per reader by mapping each term to its subdomain and checking their knows/does_not_know lists against the band rules, nudging mixed subdomains into the required overlap ranges and keeping each archetype's overall band shape (well_informed high with one soft spot, partially mixed, barely/disengaged low with small bumps for their few known terms).
- `premier_league`: The Premier League vocabulary splits naturally into transfer-market business, injury/fitness terminology, squad/matchday language and preseason training jargon, with a clearly off-topic cluster of American-football terms grouped separately since they don't belong to football coverage. Amara's transfer-market weight was raised to 0.75 to sit within the well_informed band while still staying below 0.8 to respect her does_not_know term 'contract extension'. The other readers' weights were left as set from their knows/does_not_know lists and bios, reflecting Tomasz's headline-level fluency, and the barely/disengaged readers' shallow, event-driven awareness.

### Cost

- Total API cost of generation (metered from every SDK response's usage, all runs that contributed to the cache): **$10.37** over 147 calls, 110 web searches, 3,052,426 input tokens (cache reads/writes included), 272,559 output tokens.
  - stage A (search, verification, materiality): $9.33
  - stage B (personas): $0.79
  - stage C (subdomain backfill): $0.25
  - run `persona_gen_20260914T045448Z`: $0.56 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T140759Z`: $1.01 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T142029Z`: $1.05 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T142735Z`: $0.01 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T144924Z`: $0.23 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T145329Z`: $0.44 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T154815Z`: $2.46 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T163127Z`: $1.01 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T163828Z`: $0.99 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T164359Z`: $1.13 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T171448Z`: $0.67 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T192725Z`: $0.13 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T192913Z`: $0.20 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T193145Z`: $0.12 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T193325Z`: $0.10 (rows in `judgment_log` carry this `run_id`)
  - run `persona_gen_20260914T203550Z`: $0.25 (rows in `judgment_log` carry this `run_id`)

### Things to look at first

1. **The judge's materiality verdicts.** They are the fixture's answer key for the live materiality metric and nobody has checked them; borderline scores (40-60) are where a human will most often disagree. The score and reason are in the tables above.
2. **The term splits.** A `does_not_know` term the archetype would plainly hold (or a `knows` term a beginner would not) skews ledger precision/recall for that persona. Also check that `quantities` are genuinely numeric measures.
3. **Group names carry the persona's first name** (`Premier League (Amara)`), because the fixture loader requires group names to be unique per suite. Under `--live` that string reaches the briefing prompt. It leaks no ground truth, but it is a visible artefact of the generator.
