#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import json
import os
import numpy as np
import pandas as pd
import copy
#from plotInfo import plotEvolutionSummary
from bayes_opt import BayesianOptimization
from multiprocessing import Pool
import multiprocessing as mp

from promoterz.statistics import write_evolution_logs
import promoterz
from Settings import getSettings
import promoterz.evaluation.gekko as gekkoWrapper
import chart

dict_merge = lambda a,b: a.update(b) or a
gsettings = getSettings()['Global']
settings = getSettings()['bayesian']

Strategy = settings["Strategy"]
StratConfig = getSettings()["strategies"][Strategy]

percentiles = np.array([0.25, 0.5, 0.75])
all_val = []
stats = []
candleSize = 0
historySize = 0

def EvaluateRaw(watch, DateRange, Individual, Strategy):
    config = expandGekkoStrategyParameters(Individual, Strategy)
    config["watch"] = watch
    gekko_config = gekkoWrapper.createConfig(config, DateRange)
    url = gekkoWrapper.getURL('/api/backtest')
    if candleSize > 0:
        gekko_config["gekkoConfig"]["tradingAdvisor"] = {
                "enabled": True,
                "method": Strategy,
                "candleSize": candleSize,
                "historySize": historySize,
        }
    return gekkoWrapper.httpPost(url, gekko_config)

def Evaluate(watch, DateRange, Individual, Strategy):
    config = expandGekkoStrategyParameters(Individual, Strategy)
    config["watch"] = watch
    result =gekkoWrapper.runBacktest("http://localhost:3000", config, DateRange)

def expandGekkoStrategyParameters(IND, Strategy):
    config = {}

    IND = promoterz.functions.expandNestedParameters(IND)

    config[Strategy] = IND
    return config

def evaluate_random(Strategy, parameters):

    DateRange = gekkoWrapper.getRandomDateRange(DatasetRange, deltaDays=settings['deltaDays'])
    params = expandGekkoStrategyParameters(parameters, Strategy)

    return gekkoWrapper.Evaluate(dict, 30, DateRange, params, "http://localhost:3000")

def gekko_search(**parameters):

    parallel = settings['parallel']
    num_rounds = settings['num_rounds']

    # remake CS & HS variability;
    candleSize= settings['candleSize']
    historySize= settings['historySize']

    if parallel:
        p = Pool(mp.cpu_count())
        param_list = list([(Strategy, parameters),] * num_rounds)
        scores = p.starmap(evaluate_random, param_list)
        p.close()
        p.join()
    else:
        scores = [evaluate_random(Strategy, parameters) for n in range(num_rounds)]

    print(scores)
    series = pd.Series(scores)
    mean = series.mean()
    stats.append([series.count(), mean, series.std(), series.min()] +
         [series.quantile(x) for x in percentiles] + [series.max()])
    all_val.append(mean)
    write_evolution_logs(len(all_val), stats[-1])
    return mean

def flatten_dict(d):
    def items():
        for key, value in d.items():
            if isinstance(value, dict):
                for subkey, subvalue in flatten_dict(value).items():
                    yield key + "." + subkey, subvalue
            else:
                yield key, value

    return dict(items())

def gekko_bayesian(indicator=None):
    print("")
    global Strategy
    Strategy = indicator

    watch = settings["watch"]
    global DatasetRange
    DatasetRange = gekkoWrapper.getAvailableDataset(watch)

    if indicator == None:
        Strategy = settings['Strategy']
    print("Starting search %s parameters" % Strategy)
    bo = BayesianOptimization(gekko_search, copy.deepcopy(StratConfig))

    # 1st Evaluate
    print("")
    print("Step 1: BayesianOptimization parameter search")
    bo.maximize(init_points=settings['init_points'], n_iter=settings['num_iter'])
    max_val = bo.res['max']['max_val']
    index = all_val.index(max_val)
    s1 = stats[index]

    # 2nd Evaluate
    print("")
    print("Step 2: testing searched parameters on random date")
    chosenRange = gekkoWrapper.getAvailableDataset()
    DateRange = gekkoWrapper.getDateRange(chosenRange, deltaDays=settings['testDays'])
    max_params = bo.res['max']['max_params'].copy()
    #max_params["persistence"] = 1
    print("Starting Second Evaluation")
    gekko_search(**max_params)
    s2 = stats[-1]

    # 3rd Evaluate
    print("")
    print("Step 3: testing searched parameters on recent date")
    watch = settings["watch"]
    result = EvaluateRaw(watch, DateRange, max_params, Strategy)
    if type(result['report']) == bool:
        s3 = 0.
    else:
        s3 = result['report']['relativeProfit']
    resultjson = expandGekkoStrategyParameters(max_params, Strategy)[Strategy]

    # config.js like output
    percentiles = np.array([0.25, 0.5, 0.75])
    formatted_percentiles = [str(int(round(x*100)))+"%" for x in percentiles]
    stats_index = (['count', 'mean', 'std', 'min'] +
          formatted_percentiles + ['max'])
    print("")
    print("// "+'-'*50)
    print("// "+ Strategy + ' Settings')
    print("// "+'-'*50)
    print("// 1st Evaluate: %.3f" % s1[1])
    for i in range(len(s1)):
        print('// %s: %.3f' % (stats_index[i], s1[i]))
    print("// "+'-'*50)
    print("// 2nd Evaluate: %.3f" % s2[1])
    for i in range(len(s2)):
        print('// %s: %.3f' % (stats_index[i], s2[i]))
    print("// "+'-'*50)
    print("// 3rd Evaluate nearest date: %s to %s" % (DateRange['from'], DateRange['to']))
    print("// Evaluted Score: %f" % s3)
    print("// "+'-'*50)
    print("config.%s = {%s};" % (Strategy, json.dumps(resultjson, indent=2)[1:-1]))
    print("// "+'-'*50)

    if settings["save"]:
        paramsdf = pd.DataFrame.from_dict(bo.res["all"]["params"])
        valuesdf = pd.DataFrame.from_dict(bo.res["all"]["values"])
        paramsdf["target"] = valuesdf
        watch = settings["watch"]
        filename = "_".join([watch["exchange"], watch["currency"], watch["asset"], Strategy, datetime.datetime.now().strftime('%Y%m%d_%H%M%S'), str(max_val)])
        save_dir = gsettings["save_dir"]
        csv_filename = os.path.join(save_dir, filename) + "_bayes.csv"
        json_filename = os.path.join(save_dir, filename) + "_config.json"
        json2_filename = os.path.join(save_dir, filename) + "_response.json"
        config = expandGekkoStrategyParameters(max_params, Strategy)
        config["watch"] = watch
        gekko_config = gekkoWrapper.createConfig(config, DateRange)

        if not os.path.exists(save_dir):
            os.mkdir(save_dir)
        paramsdf.to_csv(csv_filename, index=False)
        print("Saved: " + csv_filename)
        f = open(json_filename, "w")
        f.write(json.dumps(gekko_config, indent=2))
        f.close()
        print("Saved: " + json_filename)
        f = open(json2_filename, "w")
        f.write(json.dumps(result, indent=2))
        f.close()
        print("Saved: " + json2_filename)
    if settings["show_chart"]:
        chart.show_candles(result, max_params)

    return max_params

