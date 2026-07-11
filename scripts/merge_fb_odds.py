#!/usr/bin/env python3
"""将 football-data.co.uk 历史赔率合并到 FB 特征流水线。

步骤:
1. 队名模糊匹配 (football-data → 特征数据名)
2. Pinnacle 赔率去 vig（获取真实隐含概率）
3. 合并到 fb_features.csv
4. 验证覆盖质量

输出:
  data/processed/fb_features_with_odds.csv — 含赔率列的完整特征
"""
import json, sys, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(message)s')

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
import numpy as np
from rapidfuzz import fuzz, process as fuzz_process

from config.logging_config import get_logger
logger = get_logger(__name__)


def load_our_teams():
    fb = pd.read_csv('data/processed/fb_features.csv')
    teams = sorted(set(pd.concat([fb['home'], fb['away']]).dropna().unique()))
    return teams, fb


def load_fd_teams():
    odds = pd.read_csv('data/raw/fb_odds_raw.csv', low_memory=False)
    fd_teams = sorted(set(
        x for x in pd.concat([odds['HomeTeam'], odds['AwayTeam']]).dropna().unique()
        if isinstance(x, str)
    ))
    return fd_teams, odds


def build_team_mapping(our_teams, fd_teams, threshold=75):
    """构建队名映射：先手动覆写，再模糊匹配。

    策略：
    1. 从手动映射表加载已知映射
    2. 对剩余 teams 做 fuzzy match（score >= threshold）
    3. 返回映射 dict
    """
    manual = _build_overrides()

    # 反转映射：有些手动映射是 FD名→我们的名
    # 也需要反向查
    mapping = {}
    for fd_name, our_name in manual.items():
        mapping[fd_name] = our_name

    # 对剩余未映射的做 fuzzy
    remaining = [t for t in fd_teams if t not in mapping]
    fuzzy_matched = 0
    for fd_name in remaining:
        best, score, _ = fuzz_process.extractOne(fd_name, our_teams,
                                                  scorer=fuzz.token_sort_ratio)
        if score >= threshold:
            mapping[fd_name] = best
            fuzzy_matched += 1

    logger.info(f'队名映射: {len(manual)} 手动 + {fuzzy_matched} 模糊 = {len(mapping)}/{len(fd_teams)}')

    # 检查我们数据中的球队是否都有映射
    unmapped_ours = set()
    for t in our_teams:
        if t not in mapping.values():
            unmapped_ours.add(t)
    if unmapped_ours:
        logger.info(f'未映射到赔率的我们的球队: {len(unmapped_ours)} 支 (不影响已有映射的匹配)')

    return mapping


def _build_overrides():
    """FD队名 → 我们的队名 手动映射."""
    return {
        # ---- 英超 ----
        'Arsenal': 'Arsenal FC',
        'Aston Villa': 'Aston Villa FC',
        'Bournemouth': 'AFC Bournemouth',
        'Brentford': 'Brentford FC',
        'Brighton': 'Brighton & Hove Albion FC',
        'Burnley': 'Burnley FC',
        'Chelsea': 'Chelsea FC',
        'Crystal Palace': 'Crystal Palace FC',
        'Everton': 'Everton FC',
        'Fulham': 'Fulham FC',
        'Ipswich': 'Ipswich Town FC',
        'Leeds': 'Leeds United FC',
        'Leicester': 'Leicester City FC',
        'Liverpool': 'Liverpool FC',
        'Luton': 'Luton Town FC',
        'Man City': 'Manchester City FC',
        'Man United': 'Manchester United FC',
        'Newcastle': 'Newcastle United FC',
        "Nott'm Forest": 'Nottingham Forest FC',
        'Nottm Forest': 'Nottingham Forest FC',
        'Sheffield Utd': 'Sheffield United FC',
        'Sheffield Wed': 'Sheffield Wednesday FC',
        'Southampton': 'Southampton FC',
        'Stoke': 'Stoke City FC',
        'Sunderland': 'Sunderland AFC',
        'Tottenham': 'Tottenham Hotspur FC',
        'Watford': 'Watford FC',
        'West Brom': 'West Bromwich Albion FC',
        'West Ham': 'West Ham United FC',
        'Wolves': 'Wolverhampton Wanderers FC',
        # ---- 英冠 ----
        'Birmingham': 'Birmingham City FC',
        'Blackburn': 'Blackburn Rovers FC',
        'Bristol City': 'Bristol City FC',
        'Cardiff': 'Cardiff City FC',
        'Coventry': 'Coventry City FC',
        'Derby': 'Derby County FC',
        'Huddersfield': 'Huddersfield Town FC',
        'Hull': 'Hull City AFC',
        'Middlesbrough': 'Middlesbrough FC',
        'Millwall': 'Millwall FC',
        'Norwich': 'Norwich City FC',
        'Oxford Utd': 'Oxford United FC',
        'Plymouth': 'Plymouth Argyle FC',
        'Portsmouth': 'Portsmouth FC',
        'Preston': 'Preston North End FC',
        'QPR': 'Queens Park Rangers FC',
        'Reading': 'Reading FC',
        'Rotherham': 'Rotherham United FC',
        'Sheffield United': 'Sheffield United FC',
        'Swansea': 'Swansea City AFC',
        'West Brom': 'West Bromwich Albion FC',
        'Wigan': 'Wigan Athletic FC',
        # ---- 西甲 ----
        'Alaves': 'Deportivo Alavés',
        'Almeria': 'UD Almería',
        'Ath Bilbao': 'Athletic Club',
        'Ath Madrid': 'Club Atlético de Madrid',
        'Barcelona': 'FC Barcelona',
        'Betis': 'Real Betis Balompié',
        'Cadiz': 'Cádiz CF',
        'Celta': 'RC Celta de Vigo',
        'Eibar': 'SD Eibar',
        'Elche': 'Elche CF',
        'Espanol': 'RCD Espanyol de Barcelona',
        'Getafe': 'Getafe CF',
        'Girona': 'Girona FC',
        'Granada': 'Granada CF',
        'Huesca': 'SD Huesca',
        'La Coruna': 'RC Deportivo de La Coruña',
        'Las Palmas': 'UD Las Palmas',
        'Leganes': 'CD Leganés',
        'Levante': 'Levante UD',
        'Mallorca': 'RCD Mallorca',
        'Osasuna': 'CA Osasuna',
        'Rayo Vallecano': 'Rayo Vallecano',
        'Real Madrid': 'Real Madrid CF',
        'Real Sociedad': 'Real Sociedad de Fútbol',
        'Sevilla': 'Sevilla FC',
        'Valencia': 'Valencia CF',
        'Valladolid': 'Real Valladolid CF',
        'Villarreal': 'Villarreal CF',
        # ---- 德甲 ----
        'Augsburg': 'FC Augsburg',
        'Bayern Munich': 'FC Bayern München',
        'Bochum': 'VfL Bochum 1848',
        'Darmstadt': 'SV Darmstadt 98',
        'Dortmund': 'Borussia Dortmund',
        'Dusseldorf': 'Fortuna Düsseldorf',
        'Ein Frankfurt': 'Eintracht Frankfurt',
        'Freiburg': 'SC Freiburg',
        'Gladbach': "Borussia Mönchengladbach",
        'Heidenheim': '1. FC Heidenheim 1846',
        'Hoffenheim': 'TSG 1899 Hoffenheim',
        'Holstein Kiel': 'Holstein Kiel',
        'Kaiserslautern': '1. FC Kaiserslautern',
        'Karlsruhe': 'Karlsruher SC',
        'Koln': '1. FC Köln',
        'Leverkusen': 'Bayer 04 Leverkusen',
        'Mainz': '1. FSV Mainz 05',
        'Mgladbach': "Borussia Mönchengladbach",
        'Munich 1860': 'TSV 1860 München',
        'Nurnberg': '1. FC Nürnberg',
        'Paderborn': 'SC Paderborn 07',
        'RB Leipzig': 'RB Leipzig',
        'Regensburg': 'SSV Jahn Regensburg',
        'Schalke 04': 'FC Schalke 04',
        'St Pauli': 'FC St. Pauli',
        'St. Pauli': 'FC St. Pauli',
        'Stuttgart': 'VfB Stuttgart',
        'Union Berlin': '1. FC Union Berlin',
        'Werder Bremen': 'SV Werder Bremen',
        'Wolfsburg': 'VfL Wolfsburg',
        # ---- 意甲 ----
        'AC Milan': 'AC Milan',
        'Atalanta': 'Atalanta BC',
        'Bologna': 'Bologna FC 1909',
        'Cagliari': 'Cagliari Calcio',
        'Como': 'Como 1907',
        'Empoli': 'Empoli FC',
        'Fiorentina': 'ACF Fiorentina',
        'Frosinone': 'Frosinone Calcio',
        'Genoa': 'Genoa CFC',
        'Inter': 'FC Internazionale Milano',
        'Juventus': 'Juventus FC',
        'Lazio': 'SS Lazio',
        'Lecce': 'US Lecce',
        'Monza': 'AC Monza',
        'Napoli': 'SSC Napoli',
        'Parma': 'Parma Calcio 1913',
        'Roma': 'AS Roma',
        'Salernitana': 'US Salernitana 1919',
        'Sampdoria': 'UC Sampdoria',
        'Sassuolo': 'US Sassuolo Calcio',
        'Spezia': 'Spezia Calcio',
        'Torino': 'Torino FC',
        'Udinese': 'Udinese Calcio',
        'Venezia': 'Venezia FC',
        'Verona': 'Hellas Verona FC',
        # ---- 法甲 ----
        'Angers': 'Angers SCO',
        'Auxerre': 'AJ Auxerre',
        'Brest': 'Stade Brestois 29',
        'Clermont': 'Clermont Foot 63',
        'Le Havre': 'Le Havre AC',
        'Lens': 'RC Lens',
        'Lille': 'LOSC Lille',
        'Lorient': 'FC Lorient',
        'Lyon': 'Olympique Lyonnais',
        'Marseille': 'Olympique de Marseille',
        'Metz': 'FC Metz',
        'Monaco': 'AS Monaco FC',
        'Montpellier': 'Montpellier HSC',
        'Nantes': 'FC Nantes',
        'Nice': 'OGC Nice',
        'Paris SG': 'Paris Saint-Germain FC',
        'Reims': 'Stade de Reims',
        'Rennes': 'Stade Rennais FC',
        'St Etienne': 'AS Saint-Étienne',
        'Strasbourg': 'RC Strasbourg Alsace',
        'Toulouse': 'Toulouse FC',
        'Troyes': 'ES Troyes AC',
        # ---- 荷甲 ----
        'Ajax': 'AFC Ajax',
        'Almere City': 'Almere City FC',
        'AZ': 'AZ',
        'Cambuur': 'SC Cambuur',
        'Emmen': 'FC Emmen',
        'Excelsior': 'Excelsior',
        'Feyenoord': 'Feyenoord Rotterdam',
        'Fortuna Sittard': 'Fortuna Sittard',
        'Go Ahead Eagles': 'Go Ahead Eagles',
        'Groningen': 'FC Groningen',
        'Heerenveen': 'SC Heerenveen',
        'Heracles': 'Heracles Almelo',
        'NEC': 'NEC Nijmegen',
        'PEC Zwolle': 'PEC Zwolle',
        'PSV': 'PSV Eindhoven',
        'RKC Waalwijk': 'RKC Waalwijk',
        'Sparta Rotterdam': 'Sparta Rotterdam',
        'Twente': 'FC Twente',
        'Utrecht': 'FC Utrecht',
        'Vitesse': 'Vitesse',
        'Volendam': 'FC Volendam',
        'Willem II': 'Willem II Tilburg',
        # ---- 葡超 ----
        'Arouca': 'FC Arouca',
        'AVS': 'AVS',
        'Benfica': 'SL Benfica',
        'Boavista': 'Boavista FC',
        'Braga': 'SC Braga',
        'Casa Pia': 'Casa Pia AC',
        'Chaves': 'GD Chaves',
        'Estoril': 'GD Estoril Praia',
        'Estrela': 'CF Estrela da Amadora',
        'Famalicao': 'FC Famalicão',
        'Farense': 'SC Farense',
        'Gil Vicente': 'Gil Vicente FC',
        'Moreirense': 'Moreirense FC',
        'Portimonense': 'Portimonense SC',
        'Porto': 'FC Porto',
        'Rio Ave': 'Rio Ave FC',
        'Santa Clara': 'CD Santa Clara',
        'Sporting': 'Sporting CP',
        'Vitoria Guimaraes': 'Vitória SC',
        'Vizela': 'Vizela FC',
        # ---- 苏超 ----
        'Aberdeen': 'Aberdeen FC',
        'Celtic': 'Celtic FC',
        'Dundee': 'Dundee FC',
        'Dundee Utd': 'Dundee United FC',
        'Hearts': 'Heart of Midlothian FC',
        'Hibernian': 'Hibernian FC',
        'Kilmarnock': 'Kilmarnock FC',
        'Livingston': 'Livingston FC',
        'Motherwell': 'Motherwell FC',
        'Rangers': 'Rangers FC',
        'Ross County': 'Ross County FC',
        'St Johnstone': "St. Johnstone FC",
        'St Mirren': "St. Mirren FC",
        # ---- 比甲 ----
        'Anderlecht': 'Anderlecht',
        'Antwerp': 'Antwerp',
        'Cercle Brugge': 'Cercle Brugge KSV',
        'Charleroi': 'Royal Charleroi SC',
        'Club Brugge': 'Club Brugge',
        'Dender': 'Dender',
        'Eupen': 'KAS Eupen',
        'Genk': 'Racing Genk',
        'Gent': 'KAA Gent',
        'Kortrijk': 'KV Kortrijk',
        'Mechelen': 'KV Mechelen',
        'Oostende': 'KV Oostende',
        'Oud-Heverlee Leuven': 'Oud-Heverlee Leuven',
        'Seraing': 'RFC Seraing',
        'St Truiden': 'Sint-Truidense',
        'St. Gilloise': 'Union St.-Gilloise',
        'Standard': 'Standard Liege',
        'Union SG': 'Union St.-Gilloise',
        'Waasland-Beveren': 'SK Beveren',
        'Waregem': 'Zulte-Waregem',
        'Westerlo': 'KVC Westerlo',
        'RWDM': 'RWDM',
        # ---- 土超 ----
        'Adana Demirspor': 'Adana Demirspor',
        'Alanyaspor': 'Alanyaspor',
        'Ankaragucu': 'MKE Ankaragücü',
        'Antalyaspor': 'Antalyaspor',
        'Basaksehir': 'Başakşehir FK',
        'Besiktas': 'Beşiktaş JK',
        'Bodrumspor': 'Bodrum FK',
        'Erzurum BB': 'BB Erzurumspor',
        'Eyupspor': 'Eyüpspor',
        'Fenerbahce': 'Fenerbahçe SK',
        'Galatasaray': 'Galatasaray SK',
        'Gaziantep': 'Gaziantep FK',
        'Goztepe': 'Göztepe SK',
        'Hatayspor': 'Hatayspor',
        'Kasimpasa': 'Kasımpaşa SK',
        'Kayserispor': 'Kayserispor',
        'Konyaspor': 'Konyaspor',
        'Pendikspor': 'Pendikspor',
        'Rizespor': 'Çaykur Rizespor',
        'Samsunspor': 'Samsunspor',
        'Sivasspor': 'Sivasspor',
        'Trabzonspor': 'Trabzonspor',
        'Umraniyespor': 'Ümraniyespor',
        'Yeni Malatyaspor': 'Yeni Malatyaspor',
        # ---- 希超 ----
        'AEK': 'AEK Athens',
        'Aris': 'Aris Thessaloniki FC',
        'Olympiakos': 'Olympiacos FC',
        'Panathinaikos': 'Panathinaikos FC',
        'PAOK': 'PAOK FC',
        # ---- 英甲/乙 ----
        'Barnsley': 'Barnsley FC',
        'Blackpool': 'Blackpool FC',
        'Bolton': 'Bolton Wanderers FC',
        'Bristol Rovers': 'Bristol Rovers FC',
        'Burton': 'Burton Albion FC',
        'Cambridge': 'Cambridge United FC',
        'Charlton': 'Charlton Athletic FC',
        'Cheltenham': 'Cheltenham Town FC',
        'Colchester': 'Colchester United FC',
        'Crawley': 'Crawley Town FC',
        'Crewe': 'Crewe Alexandra FC',
        'Doncaster': 'Doncaster Rovers FC',
        'Exeter': 'Exeter City FC',
        'Fleetwood': 'Fleetwood Town FC',
        'Gillingham': 'Gillingham FC',
        'Grimsby': 'Grimsby Town FC',
        'Harrogate': 'Harrogate Town FC',
        'Lincoln': 'Lincoln City FC',
        'Mansfield': 'Mansfield Town FC',
        'MK Dons': 'Milton Keynes Dons FC',
        'Morecambe': 'Morecambe FC',
        'Newport': 'Newport County AFC',
        'Northampton': 'Northampton Town FC',
        'Notts County': 'Notts County FC',
        'Orient': 'Leyton Orient FC',
        'Oxford': 'Oxford United FC',
        'Peterboro': 'Peterborough United FC',
        'Port Vale': 'Port Vale FC',
        'Salford': 'Salford City FC',
        'Shrewsbury': 'Shrewsbury Town FC',
        'Stevenage': 'Stevenage FC',
        'Stockport': 'Stockport County FC',
        'Swindon': 'Swindon Town FC',
        'Tranmere': 'Tranmere Rovers FC',
        'Walsall': 'Walsall FC',
        'Wrexham': 'Wrexham AFC',
        'Wycombe': 'Wycombe Wanderers FC',
        # ---- 德乙 ----
        'Bielefeld': 'Arminia Bielefeld',
        'Braunschweig': 'Eintracht Braunschweig',
        'Dresden': 'Dynamo Dresden',
        'Elversberg': 'SV Elversberg',
        'Erzgebirge Aue': 'FC Erzgebirge Aue',
        'Fürth': 'SpVgg Greuther Fürth',
        'Greuther Furth': 'SpVgg Greuther Fürth',
        'Hamburg': 'Hamburger SV',
        'Hannover': 'Hannover 96',
        'Hertha': 'Hertha BSC',
        'Magdeburg': '1. FC Magdeburg',
        'Munster': 'SC Preußen Münster',
        'Osnabruck': 'VfL Osnabrück',
        'Preußen Münster': 'SC Preußen Münster',
        'Rostock': 'FC Hansa Rostock',
        'Sandhausen': 'SV Sandhausen',
        'Ulm': 'SSV Ulm 1846',
        'Wehen': 'SV Wehen Wiesbaden',
        'Wurzburger Kickers': 'Würzburger Kickers',
        # ---- 法乙 ----
        'Ajaccio': 'AC Ajaccio',
        'Amiens': 'Amiens SC',
        'Annecy': 'FC Annecy',
        'Bastia': 'SC Bastia',
        'Bordeaux': 'FC Girondins de Bordeaux',
        'Caen': 'SM Caen',
        'Concarneau': 'US Concarneau',
        'Dijon': 'Dijon FCO',
        'Dunkerque': 'USL Dunkerque',
        'Grenoble': 'Grenoble Foot 38',
        'Guingamp': 'EA Guingamp',
        'Laval': 'Stade Lavallois',
        'Martigues': 'FC Martigues',
        'Nancy': 'AS Nancy Lorraine',
        'Niort': 'Chamois Niortais FC',
        'Paris FC': 'Paris FC',
        'Pau': 'Pau FC',
        'Quevilly Rouen': 'US Quevilly-Rouen Métropole',
        'Red Star': 'Red Star FC',
        'Rodez': 'Rodez Aveyron Football',
        'Sochaux': 'FC Sochaux-Montbéliard',
        'Valenciennes': 'Valenciennes FC',
        # ---- 西乙 ----
        'Albacete': 'Albacete Balompié',
        'Alcorcon': 'AD Alcorcón',
        'Andorra': 'FC Andorra',
        'Burgos': 'Burgos CF',
        'Cartagena': 'FC Cartagena',
        'Castellon': 'CD Castellón',
        'Cordoba': 'Córdoba CF',
        'Eldense': 'CD Eldense',
        'Ferrol': 'Racing Ferrol',
        'Huesca': 'SD Huesca',
        'Ibiza': 'UD Ibiza',
        'Lugo': 'CD Lugo',
        'Malaga': 'Málaga CF',
        'Mirandes': 'CD Mirandés',
        'Oviedo': 'Real Oviedo',
        'Ponferradina': 'SD Ponferradina',
        'Racing Santander': 'Racing Santander',
        'Sabadell': 'CE Sabadell FC',
        'Santander': 'Racing Santander',
        'Sporting Gijon': 'Sporting Gijón',
        'Tenerife': 'CD Tenerife',
        'Villarreal B': 'Villarreal CF B',
        'Zaragoza': 'Real Zaragoza',
        # ---- 荷乙 ----
        'ADO Den Haag': 'ADO Den Haag',
        'Den Haag': 'ADO Den Haag',
        'Jong Ajax': 'Jong Ajax',
        'Jong AZ': 'Jong AZ',
        'Jong PSV': 'Jong PSV',
        'Jong Utrecht': 'Jong FC Utrecht',
        'Maastricht': 'MVV Maastricht',
        'NAC Breda': 'NAC Breda',
        'Roda JC': 'Roda JC Kerkrade',
        'Telstar': 'Telstar',
        'TOP Oss': 'TOP Oss',
        'VVV': 'VVV-Venlo',
        'VVV Venlo': 'VVV-Venlo',
        'Volendam': 'FC Volendam',
        # ---- 意乙 ----
        'Bari': 'SSC Bari',
        'Brescia': 'Brescia Calcio',
        'Catanzaro': 'US Catanzaro 1929',
        'Cittadella': 'AS Cittadella',
        'Cosenza': 'Cosenza Calcio',
        'Cremonese': 'US Cremonese',
        'Frosinone': 'Frosinone Calcio',
        'Lecco': 'Calcio Lecco 1912',
        'Modena': 'Modena FC',
        'Palermo': 'Palermo FC',
        'Parma': 'Parma Calcio 1913',
        'Perugia': 'Perugia Calcio',
        'Pescara': 'Pescara Calcio',
        'Pisa': 'AC Pisa 1909',
        'Reggiana': 'AC Reggiana 1919',
        'Salernitana': 'US Salernitana 1919',
        'Sampdoria': 'UC Sampdoria',
        'Sudtirol': 'FC Südtirol',
        'Ternana': 'Ternana Calcio',
        'Venezia': 'Venezia FC',
        'Vicenza': 'LR Vicenza',
    }


def load_odds():
    odds = pd.read_csv('data/raw/fb_odds_raw.csv', low_memory=False)
    # Only keep rows with Pinnacle odds
    odds = odds[odds['PSH'].notna() & (odds['PSH'] != '')].copy()
    # Parse date (DD/MM/YY)
    odds['parsed_date'] = pd.to_datetime(odds['Date'], dayfirst=True, errors='coerce')
    # Drop rows with invalid dates
    odds = odds.dropna(subset=['parsed_date'])
    # Convert odds to float
    for c in ['PSH', 'PSD', 'PSA']:
        odds[c] = pd.to_numeric(odds[c], errors='coerce')
    odds = odds.dropna(subset=['PSH', 'PSD', 'PSA'])
    return odds


def calculate_vig_free(odds_row):
    """去掉 Pinnacle 的 vigorish，计算真实隐含概率。"""
    h, d, a = odds_row['PSH'], odds_row['PSD'], odds_row['PSA']
    implied_h = 1.0 / h
    implied_d = 1.0 / d
    implied_a = 1.0 / a
    vig = implied_h + implied_d + implied_a - 1.0
    # 无vig概率
    fair_h = implied_h / (1.0 + vig)
    fair_d = implied_d / (1.0 + vig)
    fair_a = implied_a / (1.0 + vig)
    return fair_h, fair_d, fair_a, vig


def merge_odds():
    logger.info('=' * 60)
    logger.info('整合 football-data.co.uk 赔率到 FB 特征')
    logger.info('=' * 60)

    # 加载数据
    our_teams, fb = load_our_teams()
    logger.info(f'FB 特征: {len(fb)} 行, {len(our_teams)} 支球队')

    odds = load_odds()
    logger.info(f'赔率数据: {len(odds)} 行 (含 Pinnacle)')

    fd_teams = sorted(set(
        x for x in pd.concat([odds['HomeTeam'], odds['AwayTeam']]).dropna().unique()
        if isinstance(x, str)
    ))
    logger.info(f'football-data 队名: {len(fd_teams)} 个')

    # 构建队名映射
    mapping = build_team_mapping(our_teams, fd_teams, threshold=75)

    # 应用映射到赔率数据
    odds['home_team'] = odds['HomeTeam'].map(mapping)
    odds['away_team'] = odds['AwayTeam'].map(mapping)
    unmapped = odds['home_team'].isna().sum() + odds['away_team'].isna().sum()
    odds = odds.dropna(subset=['home_team', 'away_team'])
    logger.info(f'映射后: {len(odds)} 行 (去掉 {unmapped} 行未映射)')

    # 获取 FB 特征数据中的日期
    fb['date'] = pd.to_datetime(fb['date'], utc=True, errors='coerce')

    # 合并赔率（统一 tz-naive）
    odds['match_date'] = odds['parsed_date'].dt.normalize()
    fb['match_date'] = fb['date'].dt.tz_localize(None).dt.normalize()

    merged = fb.merge(
        odds[['match_date', 'home_team', 'away_team', 'PSH', 'PSD', 'PSA',
              'FTHG', 'FTAG', 'FTR', '_season', '_league']],
        how='left',
        left_on=['match_date', 'home', 'away'],
        right_on=['match_date', 'home_team', 'away_team'],
        suffixes=('', '_odds')
    )

    matched = merged['PSH'].notna().sum()
    logger.info(f'合并后: {len(merged)} 行, 匹配赔率: {matched}/{len(fb)} ({matched/len(fb)*100:.1f}%)')

    # 计算去vig概率
    odds_mask = merged['PSH'].notna()
    h_probs, d_probs, a_probs, vigs = [], [], [], []
    for _, row in merged[odds_mask].iterrows():
        fh, fd, fa, v = calculate_vig_free(row)
        h_probs.append(fh)
        d_probs.append(fd)
        a_probs.append(fa)
        vigs.append(v)

    merged['pinnacle_h'] = merged['PSH']
    merged['pinnacle_d'] = merged['PSD']
    merged['pinnacle_a'] = merged['PSA']
    merged['fair_h_prob'] = np.nan
    merged['fair_d_prob'] = np.nan
    merged['fair_a_prob'] = np.nan
    merged['vig'] = np.nan

    merged.loc[odds_mask, 'fair_h_prob'] = h_probs
    merged.loc[odds_mask, 'fair_d_prob'] = d_probs
    merged.loc[odds_mask, 'fair_a_prob'] = a_probs
    merged.loc[odds_mask, 'vig'] = vigs

    avg_vig = merged['vig'].mean()
    logger.info(f'平均 Vig: {avg_vig:.4f} ({avg_vig*100:.2f}%)')

    # 添加隐含概率特征
    merged['implied_home_win'] = merged['fair_h_prob']
    merged['implied_draw'] = merged['fair_d_prob']
    merged['implied_away_win'] = merged['fair_a_prob']
    merged['implied_home_edge'] = np.where(
        merged['fair_h_prob'].notna(),
        1.0 / merged['fair_h_prob'] * merged['pinnacle_h'] - 1.0,
        np.nan
    )

    # 保存
    out_cols = list(fb.columns) + ['pinnacle_h', 'pinnacle_d', 'pinnacle_a',
                                     'fair_h_prob', 'fair_d_prob', 'fair_a_prob', 'vig']
    # Only keep actual columns
    out_cols = [c for c in out_cols if c in merged.columns]

    out_path = 'data/processed/fb_features_with_odds.csv'
    merged[out_cols].to_csv(out_path, index=False)
    logger.info(f'已保存: {out_path} ({len(merged)} 行, {len(out_cols)} 列)')

    # 按赛季统计匹配率
    fb['_season_tag'] = fb['date'].dt.year.astype(str)
    merged['_season_tag'] = merged['date'].dt.year.astype(str)
    for yr in sorted(fb['_season_tag'].unique()):
        total = (fb['_season_tag'] == yr).sum()
        matched_yr = (merged['_season_tag'] == yr) & merged['PSH'].notna()
        if total > 0:
            logger.info(f'  {yr}: {matched_yr.sum()}/{total} ({matched_yr.sum()/total*100:.0f}%)')

    return merged


def main():
    merge_odds()


if __name__ == '__main__':
    main()
