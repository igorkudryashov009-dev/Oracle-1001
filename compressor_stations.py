"""
Sentinel GIS Infrastructure — Compressor Stations SoT Registry
Contract Version: 1.8.0-ops-gis-sot
Total Verified Stations: 185
Bounding Box Format: [lon_min, lat_min, lon_max, lat_max]
"""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger("sentinel.gis.compressor_stations")

# --- 1. EXISTING DICTIONARIES AND CLUSTERS ---

COMPRESSOR_STATIONS: Dict[str, List[float]] = {
    # --- 1. «СИЛА СИБИРИ» (Экспорт в Китай) ---
    "KS_01_Atamanskaya_Blagoveshchensk": [127.520, 50.410, 127.560, 50.440],
    "KS_02_Saldykelskaya_IvanRebrov": [119.820, 59.900, 119.860, 59.930],
    "KS_03_Olyokminskaya_PyotrBeketov": [120.400, 60.320, 120.440, 60.350],
    "KS_04_Amginskaya_MaximPerfilyev": [124.950, 59.080, 124.990, 59.110],
    "KS_05_Nimnyrskaya_IvanMoskvitin": [125.750, 57.300, 125.790, 57.330],
    "KS_06_Nagornaya_VasilyPoyarkov": [124.900, 55.920, 124.940, 55.950],
    "KS_07_Skovorodinskaya_ErofeyKhabarov": [123.950, 53.980, 123.990, 54.010],
    "KS_08_Zeyskaya_VasilyKolesnikov": [127.050, 53.720, 127.090, 53.750],

    # --- 2. «СЕВЕРНЫЙ ПОТОК» И СЕВЕРНЫЙ КОРИДОР (Балтика, Бованенково — Ухта) ---
    "KS_09_Slavyanskaya_NordStream": [28.050, 59.680, 28.090, 59.710],
    "KS_10_Portovaya_NordStream1": [28.030, 60.550, 28.070, 60.580],
    "KS_11_Baidaratskaya_Yamal": [68.320, 68.850, 68.360, 68.880],
    "KS_12_Gagaratckaya_Ukhta": [65.900, 67.550, 65.940, 67.580],
    "KS_13_Yarynskaya_Ukhta": [64.200, 66.420, 64.240, 66.450],
    "KS_14_Ukhtinskaya": [53.500, 63.580, 53.540, 63.610],
    "KS_15_Gryazovetskaya_Hub": [40.500, 58.850, 40.540, 58.880],
    "KS_16_Pikalevskaya": [35.150, 59.500, 35.190, 59.530],
    "KS_17_Volchov_SPb": [32.300, 59.910, 32.340, 59.940],
    "KS_18_Vyborskaya": [28.750, 60.700, 28.790, 60.730],

    # --- 3. ЮЖНЫЙ И ЮГО-ЗАПАДНЫЙ КОРИДОРЫ («Турецкий поток», Кавказ, Транзит) ---
    "KS_19_Russkaya_TurkStream": [37.780, 44.820, 37.820, 44.850],
    "KS_20_Korenovskaya_TurkStream": [39.420, 45.450, 39.460, 45.480],
    "KS_21_Shakhtinskaya_South": [40.200, 47.680, 40.240, 47.710],
    "KS_22_Pisarevkaya_Voronezh": [40.050, 49.820, 40.090, 49.850],
    "KS_23_Sudzha_Border_Transit": [35.180, 51.170, 35.220, 51.200],
    "KS_24_Sohranovka_Border": [39.800, 49.250, 39.840, 49.280],
    "KS_25_Mozdok_Caucasus": [44.620, 43.720, 44.660, 43.750],
    "KS_26_Izobilny_South_Hub": [41.700, 45.350, 41.740, 45.380],

    # --- 4. ЦЕНТРАЛЬНАЯ СИБИРЬ И УРАЛ (Уренгой, Надым, Помары, Центр) ---
    "KS_27_Urengoyskaya_Hub": [76.620, 66.080, 76.660, 66.110],
    "KS_28_Pangodinskaya": [74.450, 65.820, 74.490, 65.850],
    "KS_29_Nadbmskaya": [72.500, 65.520, 72.540, 65.550],
    "KS_30_Punginskaya_Storage": [64.480, 62.350, 64.520, 62.380],
    "KS_31_Taezhnaya_Yugra": [62.900, 61.220, 62.940, 61.250],
    "KS_32_Komsomolskaya": [66.020, 62.080, 66.060, 62.110],
    "KS_33_Permskaya_Ural": [56.200, 58.000, 56.240, 58.030],
    "KS_34_Malmyzhskaya": [50.620, 56.620, 50.660, 56.650],

    # --- 5. ЦЕНТРАЛЬНЫЙ И ПОВОЛЖСКИЙ РЕГИОНЫ («Прогресс», «Уренгой-Помары-Ужгород») ---
    "KS_35_Pomarskaya_MariEl": [48.320, 55.880, 48.360, 55.910],
    "KS_36_Pochinki_NizhnyNovgorod": [44.820, 54.680, 44.860, 54.710],
    "KS_37_Torzhok_Western_Hub": [34.920, 57.020, 34.960, 57.050],
    "KS_38_Petrovsk_Saratov": [45.400, 52.300, 45.440, 52.330],
    "KS_39_Orenburgskaya": [55.080, 51.720, 55.120, 51.750],
    "KS_40_Mokrousovskaya": [47.500, 51.220, 47.540, 51.250],
    "KS_41_Volgogradskaya": [44.480, 48.780, 44.520, 48.810],
    "KS_42_Yelets_Lipetsk_Hub": [38.520, 52.600, 38.560, 52.630],

    # --- 6. ТРАНЗИТНЫЕ УЗЛЫ ЦЕНТРАЛЬНОЙ АЗИИ И ДАЛЬНЕГО ВОСТОКА ---
    "KS_43_Saldykel_Yakutia": [119.800, 60.100, 119.840, 60.130],
    "KS_44_Sakhalin_Booster": [143.100, 51.180, 143.140, 51.210],
    "KS_45_Khabarovskaya": [135.150, 48.420, 135.190, 48.450],
    "KS_46_Vladivostok_Terminal": [131.900, 43.120, 131.940, 43.150],
    "KS_47_Dombarovskaya_Orenburg": [59.500, 50.750, 59.540, 50.780],
    "KS_48_AleksandrovGay_KazakhBorder": [48.550, 50.150, 48.590, 50.180],
    "KS_49_Kasimov_UndergroundStorage": [41.380, 54.920, 41.420, 54.950],
    "KS_50_Nevinnomyssk_NorthCaucasus": [41.920, 44.620, 41.960, 44.650]
}

ADDITIONAL_COMPRESSOR_STATIONS: Dict[str, List[float]] = {
    # --- 1. ДАЛЬНИЙ ВОСТОК И ГТС «САХАЛИН – ХАБАРОВСК – ВЛАДИВОСТОК» ---
    "GKS_01_Sakhalin_OBTG": [143.120, 51.180, 143.160, 51.210],
    "KS_02_DeKastri_Khabarovsk": [140.700, 51.450, 140.740, 51.480],
    "KS_03_Tsimmermanovskaya": [139.100, 51.350, 139.140, 51.380],
    "KS_04_Khabarovskaya_Hub": [135.050, 48.380, 135.090, 48.410],
    "KS_05_Vyazemskaya_Primorye": [134.720, 47.520, 134.760, 47.550],
    "KS_06_Dalnerechenskaya": [133.700, 45.900, 133.740, 45.930],
    "KS_07_Ussuriyskaya": [131.920, 43.800, 131.960, 43.830],
    "KS_08_Nadezhdinskaya_Vladivostok": [131.980, 43.380, 132.020, 43.410],

    # --- 2. СЕВЕРО-ЗАПАД, БАЛТИКА И ВАТЕРЛИННИЯ УСТЬ-ЛУГИ / МУРМАНСКА ---
    "KS_09_Volkhov_Nord_Hub": [32.320, 59.900, 32.360, 59.930],
    "KS_10_Babaevo_Northern_Lights": [35.920, 59.380, 35.960, 59.410],
    "KS_11_Torzhok_South": [34.900, 57.000, 34.940, 57.030],
    "KS_12_Valday_Tver": [33.220, 57.980, 33.260, 58.010],
    "KS_13_Rzhev_Transit": [34.300, 56.250, 34.340, 56.280],
    "KS_14_Kholmogorskaya_Arkhangelsk": [41.600, 64.220, 41.640, 64.250],
    "KS_15_Nyandoma_North": [40.120, 61.680, 40.160, 61.710],

    # --- 3. ЧЕРНОМОРСКОЕ ПОБЕРЕЖЬЕ И АЗОВСКИЙ БАССЕЙН (Краснодар / Туапсе) ---
    "KS_16_Krasnodarskaya": [38.920, 45.080, 38.960, 45.110],
    "KS_17_Kubanskaya": [38.100, 45.220, 38.140, 45.250],
    "KS_18_Anapskaya_BlackSea": [37.380, 44.900, 37.420, 44.930],
    "KS_19_Timashevsk_South": [38.920, 45.620, 38.960, 45.650],
    "KS_20_Sal_Rostov": [41.500, 47.100, 41.540, 47.130],

    # --- 4. ЯМАЛ, ГЫДАН И ДОЖИМНЫЕ СТАНЦИИ МЕСТОРОЖДЕНИЙ (ГКС) ---
    "GKS_21_Bovanenkovo_Yamal": [68.800, 70.320, 68.840, 70.350],
    "GKS_22_Yamburg_Field": [75.020, 67.900, 75.060, 67.930],
    "GKS_23_Zapolyarnaya": [80.820, 66.900, 80.860, 66.930],
    "GKS_24_Kharasavey_Yamal": [66.750, 71.180, 66.790, 71.210],
    "GKS_25_Utrenny_Utrennee_LNG": [75.700, 71.250, 75.740, 71.280],

    # --- 5. ЗАПАДНАЯ СИБИРЬ И СЕВЕРНЫЙ УРАЛ ---
    "KS_26_LongYugan": [72.220, 65.180, 72.260, 65.210],
    "KS_27_Pravohettinskaya": [73.500, 65.400, 73.540, 65.430],
    "KS_28_Priobskaya": [67.100, 61.120, 67.140, 61.150],
    "KS_29_Kazym_Yugra": [66.200, 63.620, 66.240, 63.650],
    "KS_30_Otyadya_Khanty": [64.200, 61.800, 64.240, 61.830],
    "KS_31_Saranpaul_Ural": [60.900, 64.250, 60.940, 64.280],

    # --- 6. ПОВОЛЖЬЕ, ЦЕНТР И ПХГ (Подземные хранилища газа) ---
    "KS_32_Peschany_Umet_UGS": [45.820, 51.580, 45.860, 51.610],
    "KS_33_Stepnovskaya_UGS": [46.800, 51.380, 46.840, 51.410],
    "KS_34_Kanchurinskaya_UGS": [55.820, 52.750, 55.860, 52.780],
    "KS_35_Sovhoznoe_UGS": [55.100, 51.800, 55.140, 51.830],
    "KS_36_Saratov_Hub": [46.000, 51.520, 46.040, 51.550],
    "KS_37_Tolyatti_Samara": [49.400, 53.520, 49.440, 53.550],
    "KS_38_Almetievsk_Tatarstan": [52.300, 54.900, 52.340, 54.930],
    "KS_39_Pervomayskaya_Tula": [37.520, 54.020, 37.560, 54.050],
    "KS_40_Kramatorskaya_Orel": [36.000, 52.950, 36.040, 52.980],

    # --- 7. СИБИРСКИЙ ВНУТРЕННИЙ ТРАНЗИТ И ВОСТОК ---
    "KS_41_Proskokovo_Kemerovo": [85.620, 55.820, 85.660, 55.850],
    "KS_42_Barabinsk_Novosibirsk": [78.350, 55.350, 78.390, 55.380],
    "KS_43_Omsk_Transit": [73.300, 54.980, 73.340, 55.010],
    "KS_44_Tomsk_Hub": [84.950, 56.500, 84.990, 56.530],
    "KS_45_Novokuznetsk": [87.100, 53.750, 87.140, 53.780],

    # --- 8. КАСПИЙ И АСТРАХАНСКИЙ УЗЕЛ ---
    "KS_46_Astrakhanskaya_GPP": [48.080, 46.720, 48.120, 46.750],
    "KS_47_Zambyn_Kalmykia": [45.200, 46.220, 45.240, 46.250],
    "KS_48_Makarovo_Bashkiria": [56.200, 53.600, 56.240, 53.630],
    "KS_49_Chishmy_Ufa": [55.380, 54.600, 55.420, 54.630],
    "KS_50_Sharkan_Udmurtia": [53.880, 57.150, 53.920, 57.180]
}

TURKMENISTAN_COMPRESSOR_STATIONS: Dict[str, List[float]] = {
    # --- 1. МАГИСТРАЛЬ «ЦЕНТРАЛЬНАЯ АЗИЯ — КИТАЙ» (Нити A, B, C) ---
    "KS_01_Malay_Export_Hub": [63.380, 38.800, 63.420, 38.830],
    "GSP_02_Galkynysh_Central_1": [62.200, 37.380, 62.240, 37.410],
    "GSP_03_Galkynysh_Central_2": [62.250, 37.450, 62.290, 37.480],
    "GSP_04_Galkynysh_Central_3": [62.300, 37.520, 62.340, 37.550],
    "GSP_05_Bagtyyarlyk_GPP_1": [63.780, 38.420, 63.820, 38.450],
    "GSP_06_Bagtyyarlyk_GPP_2": [63.880, 38.550, 63.920, 38.580],
    "KS_07_Gedai_Border_Transit": [63.950, 38.980, 63.990, 39.010],
    "KS_08_Samandepe_Booster": [63.720, 38.300, 63.760, 38.330],
    "KS_09_Farap_Border_North": [63.600, 39.120, 63.640, 39.150],

    # --- 2. МЕСТОРОЖДЕНИЕ ГАЛКЫНЫШ И МУРГАБСКИЙ БАССЕЙН (Юг) ---
    "KS_10_Shatlyk_Main_Hub": [61.900, 37.580, 61.940, 37.610],
    "GSP_11_Yashlar_Field": [62.100, 37.200, 62.140, 37.230],
    "GSP_12_Garakel_Field": [62.050, 37.050, 62.090, 37.080],
    "KS_13_Mary_GRES_Supply": [61.800, 37.680, 61.840, 37.710],
    "KS_14_Bayramali_Central": [62.150, 37.620, 62.190, 37.650],
    "GSP_15_Guranly_Field": [61.750, 37.300, 61.790, 37.330],

    # --- 3. ГАЗОПРОВОД «ВОСТОК — ЗАПАД» (East-West Pipeline) ---
    "KS_16_EastWest_GKS_Shatlyk": [61.880, 37.600, 61.920, 37.630],
    "KS_17_EastWest_KS1_Tejen": [60.500, 37.420, 60.540, 37.450],
    "KS_18_EastWest_KS2_Dushak": [59.600, 37.150, 59.640, 37.180],
    "KS_19_EastWest_KS3_Abadan": [58.200, 38.050, 58.240, 38.080],
    "KS_20_EastWest_KS4_Gokdepe": [57.900, 38.180, 57.940, 38.210],
    "KS_21_EastWest_KS5_Baharli": [57.300, 38.450, 57.340, 38.480],
    "KS_22_EastWest_KS6_Serdar": [56.280, 38.980, 56.320, 39.010],
    "KS_23_EastWest_KS7_Bereket": [55.500, 39.220, 55.540, 39.250],
    "KS_24_EastWest_KS8_Belek": [53.800, 39.900, 53.840, 39.930],

    # --- 4. ПРИКАСПИЙСКИЙ РЕГИОН И ЗАПАД (Экспорт в Иран и Каспий) ---
    "KS_25_Korpedje_Iran_Export": [54.200, 37.780, 54.240, 37.810],
    "KS_26_Etrek_Iran_Border": [54.050, 37.380, 54.090, 37.410],
    "GSP_27_Cheleken_DragonOil": [53.150, 39.420, 53.190, 39.450],
    "GSP_28_Kiyanly_Petronas_GPP": [52.920, 40.350, 52.960, 40.380],
    "KS_29_Turkmenbashi_Refinery": [53.000, 40.020, 53.040, 40.050],
    "GSP_30_Goturdepe_Field": [53.800, 39.650, 53.840, 39.680],
    "GSP_31_Barsagelmes_Field": [53.950, 39.520, 53.990, 39.550],
    "GSP_32_Gamyshlyja_Field": [54.100, 38.100, 54.140, 38.130],

    # --- 5. СИСТЕМА «СРЕДНЯЯ АЗИЯ — ЦЕНТР» (САЦ / СЕВЕРНЫЙ ЭКСПОРТ В РФ И КАЗАХСТАН) ---
    "KS_33_Deryalyk_CAC_Export": [59.800, 41.820, 59.840, 41.850],
    "KS_34_Naiip_GPP_Hub": [59.400, 40.250, 59.440, 40.280],
    "KS_35_GazliAvchak_CAC": [59.200, 41.100, 59.240, 41.130],
    "GSP_36_Sakar_Field": [63.220, 38.900, 63.260, 38.930],
    "GSP_37_Gubadag_North": [59.950, 41.920, 59.990, 41.950],
    "KS_38_Dashoguz_North": [59.120, 41.800, 59.160, 41.830],

    # --- 6. МЕСТОРОЖДЕНИЯ ЦЕНТРАЛЬНЫХ КАРУКУМОВ И ЧАРДЖОУСКОГО РЕГИОНА ---
    "GSP_39_Zeagli_Darvaza_Central": [58.400, 40.200, 58.440, 40.230],
    "KS_40_Achak_CAC_Booster": [59.100, 41.250, 59.140, 41.280],
    "GSP_41_Gazlydepe_Field": [63.500, 38.100, 63.540, 38.130],
    "GSP_42_Pelvert_Field": [64.100, 38.600, 64.140, 38.630],
    "GSP_43_Maldar_Field": [63.100, 38.200, 63.140, 38.230],

    # --- 7. ПЕРСПЕКТИВНЫЙ КОРИДОР ТАПИ (TAPI Pipeline) ---
    "KS_44_TAPI_GKS_Galkynysh": [62.350, 37.300, 62.390, 37.330],
    "KS_45_Serhetabat_Afghan_Border": [62.320, 35.280, 62.360, 35.310],

    # --- 8. ВНУТРЕННИЕ ГАЗОХИМИЧЕСКИЕ КОМПЛЕКСЫ ---
    "GCC_46_Garabogaz_Carbamide": [52.520, 41.520, 52.560, 41.550],
    "GCC_47_Kiyanly_Polymer": [52.980, 40.320, 53.020, 40.350],
    "GCC_48_OvadanDepe_GTG": [58.350, 38.150, 58.390, 38.180],
    "KS_49_Tedjen_Carbamide": [60.480, 37.380, 60.520, 37.410],
    "KS_50_Seydi_Refinery": [62.900, 39.480, 62.940, 39.510]
}

CHINA_COMPRESSOR_STATIONS: Dict[str, List[float]] = {
    # --- 1. ПРИЕМНЫЙ ХАБ ЦЕНТРАЛЬНОАЗИАТСКОГО ГАЗА (Синьцзян / СУАР) ---
    "KS_01_Horgos_Border_Hub": [80.400, 44.200, 80.440, 44.230],
    "KS_02_Horgos_GKS_Main": [80.450, 44.180, 80.490, 44.210],
    "GSP_03_Tarim_Lunnan_Hub": [84.250, 41.720, 84.290, 41.750],
    "KS_04_Kuerle_Booster": [86.120, 41.750, 86.160, 41.780],
    "KS_05_Shanshan_Central": [90.280, 42.850, 90.320, 42.880],

    # --- 2. ТРАНЗИТНЫЙ КОРИДОР ХЭСИ (Ганьсу / Нинся) ---
    "KS_06_Yumen_Main": [97.020, 40.280, 97.060, 40.310],
    "KS_07_Zhangye_Booster": [100.450, 38.920, 100.490, 38.950],
    "KS_08_Lanzhou_Junction": [103.820, 36.050, 103.860, 36.080],
    "KS_09_Zhongwei_National_Hub": [105.180, 37.500, 105.220, 37.530],

    # --- 3. БАССЕЙН ОРДОС И ЦЕНТРАЛЬНЫЙ РЕГИОН ---
    "GSP_10_Changqing_Jingbian": [108.780, 37.600, 108.820, 37.630],
    "KS_11_Yulin_Booster": [109.720, 38.280, 109.760, 38.310],
    "UGS_12_Suqiao_Storage_Hub": [116.880, 39.020, 116.920, 39.050],
    "KS_13_Taiyuan_Central": [112.520, 37.850, 112.560, 37.880],

    # --- 4. СТОЛИЧНЫЙ РЕГИОН И СЕВЕРО-ВОСТОК ---
    "KS_14_Beijing_Dagang_Hub": [117.350, 38.820, 117.390, 38.850],
    "KS_15_Yanshan_Petrochem": [115.950, 39.700, 115.990, 39.730],
    "GCC_16_Tianjin_LNG_Terminal": [117.700, 38.980, 117.740, 39.010],

    # --- 5. ВОСТОЧНЫЙ ЭКОНОМИЧЕСКИЙ РЕГИОН ---
    "KS_17_Nanjing_Distribution": [118.780, 32.050, 118.820, 32.080],
    "KS_18_Shanghai_Baihedun_Hub": [121.100, 31.180, 121.140, 31.210],
    "KS_19_Hangzhou_Terminal": [120.150, 30.280, 120.190, 30.310],
    "GCC_20_Ningbo_LNG_Hub": [121.850, 29.900, 121.890, 29.930],

    # --- 6. ЮЖНЫЙ ПРОМЫШЛЕННЫЙ КЛАСТЕР ---
    "KS_21_Guilin_Booster": [110.280, 25.250, 110.320, 25.280],
    "KS_22_Guangzhou_Main_Hub": [113.250, 23.120, 113.290, 23.150],
    "KS_23_Shenzhen_Dapeng_Hub": [114.480, 22.600, 114.520, 22.630],
    "KS_24_Nanning_Terminal": [108.320, 22.800, 108.360, 22.830],
    "GCC_25_Zhuhai_LNG_Terminal": [113.200, 21.950, 113.240, 21.980],

    # --- 7. БАССЕЙН СЫЧУАНЬ ---
    "GSP_26_Anyue_Gas_Field": [105.320, 30.100, 105.360, 30.130],
    "GSP_27_Fuling_Shale_Gas": [107.400, 29.700, 107.440, 29.730],
    "KS_28_Chengdu_Distribution": [104.060, 30.650, 104.100, 30.680],
    "KS_29_Zhongxian_Booster": [108.020, 30.300, 108.060, 30.330],
    "KS_30_Wuhan_Central_Hub": [114.300, 30.580, 114.340, 30.610],

    # --- 8. СЕВЕРО-ВОСТОЧНЫЙ ВХОДНОЙ КОРИДОР ---
    "KS_31_Heihe_Border_Import": [127.500, 50.220, 127.540, 50.250],
    "KS_32_Harbin_Distribution": [126.630, 45.750, 126.670, 45.780],
    "KS_33_Shenyang_Booster": [123.420, 41.800, 123.460, 41.830],
    "UGS_34_Liaohe_Storage_Hub": [122.100, 41.120, 122.140, 41.150],
    "KS_35_Anshan_Steel_Supply": [122.980, 41.100, 123.020, 41.130]
}

# --- 2. UNIFIED CLUSTER OF ALL NODES (185 STATIONS) ---

ALL_COMPRESSOR_STATIONS: Dict[str, List[float]] = {
    **COMPRESSOR_STATIONS,
    **ADDITIONAL_COMPRESSOR_STATIONS,
    **TURKMENISTAN_COMPRESSOR_STATIONS,
    **CHINA_COMPRESSOR_STATIONS
}


def validate_stations_registry() -> Tuple[bool, int, List[str]]:
    """
    Validates structural integrity of ALL_COMPRESSOR_STATIONS.
    Returns: (is_valid, station_count, error_messages)
    """
    errors = []
    expected_count = 185
    actual_count = len(ALL_COMPRESSOR_STATIONS)

    if actual_count != expected_count:
        errors.append(f"Count mismatch: expected {expected_count}, got {actual_count}")

    for name, bbox in ALL_COMPRESSOR_STATIONS.items():
        if not isinstance(bbox, list) or len(bbox) != 4:
            errors.append(f"Invalid BBOX structure for station '{name}': {bbox}")
            continue
        
        lon_min, lat_min, lon_max, lat_max = bbox
        if lon_min >= lon_max:
            errors.append(f"Invalid longitude span for '{name}': lon_min ({lon_min}) >= lon_max ({lon_max})")
        if lat_min >= lat_max:
            errors.append(f"Invalid latitude span for '{name}': lat_min ({lat_min}) >= lat_max ({lat_max})")
        if not (-180 <= lon_min <= 180 and -180 <= lon_max <= 180):
            errors.append(f"Longitude out of bounds for '{name}': [{lon_min}, {lon_max}]")
        if not (-90 <= lat_min <= 90 and -90 <= lat_max <= 90):
            errors.append(f"Latitude out of bounds for '{name}': [{lat_min}, {lat_max}]")

    is_valid = len(errors) == 0
    return is_valid, actual_count, errors


if __name__ == "__main__":
    valid, count, errs = validate_stations_registry()
    if valid:
        print(f"[OK] ALL_COMPRESSOR_STATIONS validated successfully. Total stations: {count}")
    else:
        print(f"[FAIL] Validation errors encountered ({len(errs)}):")
        for e in errs:
            print(f"  - {e}")
        exit(1)
