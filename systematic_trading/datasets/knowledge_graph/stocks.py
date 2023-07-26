"""
curl -C - -O "https://dumps.wikimedia.org/enwiki/20230620/enwiki-20230620-pages-articles-multistream.xml.bz2"
curl -C - -O "https://dumps.wikimedia.org/enwiki/20230620/enwiki-20230620-pages-articles-multistream-index.txt.bz2"
"""

from datetime import date
import json
import os
import pickle
import re
import time
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
import click
from datasets import load_dataset
import pandas as pd
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from tqdm import tqdm
from webdriver_manager.chrome import ChromeDriverManager

from systematic_trading.helpers import nasdaq_headers
from systematic_trading.datasets.knowledge_graph import KnowledgeGraph
from systematic_trading.datasets.knowledge_graph.wikipedia import Wikipedia


class Stocks(KnowledgeGraph):
    def __init__(self, tag_date: date = None, username: str = None):
        super().__init__("stocks", tag_date, username)
        self.name = f"stocks"

    def __download_nasdaq(self) -> pd.DataFrame:
        """
        Returns a DataFrame of NASDAQ stocks
        """
        url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
        response = requests.get(url, headers=nasdaq_headers())
        json_data = response.json()
        df = pd.DataFrame(data=json_data["data"]["rows"])
        df = df[["symbol", "name", "country", "sector", "industry"]]
        # filter common stocks
        index = df.name.apply(lambda x: x.endswith("Common Stock"))
        df = df.loc[index, :]
        df.reset_index(drop=True, inplace=True)
        nasdaq_names = df.name.apply(
            lambda x: x.replace(" Common Stock", "")
            .replace(" Inc.", "")
            .replace(" Inc", "")
            .replace(" Class A", "")
        )
        df.name = nasdaq_names
        df.rename(
            columns={
                "name": "security",
                "sector": "gics_sector",
                "industry": "gics_sub_industry",
            },
            inplace=True,
        )
        return df

    def __download_sp500(self) -> pd.DataFrame:
        dataset = load_dataset("edarchimbaud/index-constituents-sp500")
        df = dataset["train"].to_pandas()
        df = df[["symbol", "security", "gics_sector", "gics_sub_industry"]]
        df.loc[:, "country"] = "United States"
        return df

    def __download(self) -> pd.DataFrame:
        path_tgt = os.path.join("data", "stocks.raw.csv")
        if os.path.exists(path_tgt):
            return
        self.dataset_df = pd.concat(
            [
                self.__download_nasdaq(),
                self.__download_sp500(),
            ]
        )
        self.dataset_df = self.dataset_df.drop_duplicates(
            subset=["symbol"], keep="first"
        )
        self.dataset_df.sort_values(by=["symbol"], inplace=True)
        self.dataset_df.reset_index(drop=True, inplace=True)
        self.__save(path=path_tgt)

    def __load(self, path):
        if path.endswith(".csv"):
            self.dataset_df = pd.read_csv(path)
        elif path.endswith(".pkl"):
            self.dataset_df = pd.read_pickle(path)

    def __save(self, path):
        if path.endswith(".csv"):
            self.dataset_df.to_csv(
                path,
                index=False,
            )
        elif path.endswith(".pkl"):
            self.dataset_df.to_pickle(path)

    def __add_wikipedia_title(self):
        """
        Add wikipedia title to the DataFrame.
        """
        path_src = os.path.join("data", "stocks.raw.csv")
        path_tgt = os.path.join("data", "stocks.title.csv")
        if os.path.exists(path_tgt):
            return
        self.__load(path=path_src)
        self.dataset_df.fillna("", inplace=True)
        self.dataset_df.loc[:, "wikipedia_title"] = ""
        # Match with Google search
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/106.0.0.0 Safari/537.36"
        }
        options = Options()
        options.headless = False
        options.add_argument("user-agent=" + headers["User-Agent"])
        driver = webdriver.Chrome(ChromeDriverManager().install(), options=options)
        driver.get("https://www.google.com/")
        input("Cookies accepted?")
        path = os.path.join("data", "stocks.csv")
        for index, row in tqdm(self.dataset_df.iterrows(), total=len(self.dataset_df)):
            if index < 6:
                continue
            if row["wikipedia_title"]:
                continue
            encoded_query = quote_plus(row["security"] + " company")
            url = "https://www.google.com/search?hl=en&q=" + encoded_query
            driver.get(url)
            time.sleep(60)
            body = driver.find_element("xpath", "//body")
            body_html = body.get_attribute("innerHTML")
            soup = BeautifulSoup(body_html, "html.parser")
            hrefs = [
                a["href"]
                for a in soup.find_all("a")
                if a.has_attr("href")
                and a["href"].startswith("https://en.wikipedia.org/")
                and a.text.strip() == "Wikipedia"
            ]
            if len(hrefs) == 0:
                continue
            href = hrefs[0]
            wikipedia_name = href.split("/")[-1].replace("_", " ")
            self.dataset_df.loc[index, "wikipedia_title"] = wikipedia_name
            self.__save(path=path_tgt)
        self.__save(path=path_tgt)

    def __add_wikipedia_page(self):
        """
        Add wikipedia page to the DataFrame.
        """
        path_src = os.path.join("data", "stocks.title.csv")
        path_tgt = os.path.join("data", "stocks.page.pkl")
        if os.path.exists(path_tgt):
            return
        self.__load(path=path_src)
        titles = self.dataset_df.wikipedia_title.tolist()
        wikipedia = Wikipedia()
        pages = wikipedia.select_pages(titles)
        self.dataset_df.loc[:, "wikipedia_page"] = ""
        for index, row in self.dataset_df.iterrows():
            title = row["wikipedia_title"]
            if title == "" or title not in pages:
                continue
            row["wikipedia_page"] = pages[title]
        self.__save(path=path_tgt)

    def __add_relationships(self):
        """
        Add relationships to the DataFrame.
        """
        path_src = os.path.join("data", "stocks.page.pkl")
        path_tgt = os.path.join("data", "stocks.csv")
        if os.path.exists(path_tgt):
            return
        self.__load(path=path_src)
        self.dataset_df.loc[:, "categories"] = ""
        pattern = r"\[\[Category:(.*?)\]\]"
        for index, row in self.dataset_df.iterrows():
            text = row["wikipedia_page"]
            if text == "":
                continue
            categories = list(re.findall(pattern, text))
            self.dataset_df.loc[index, "categories"] = json.dumps(categories)
        self.dataset_df = self.dataset_df[self.expected_columns]
        self.__save(path=path_tgt)

    def set_dataset_df(self):
        """
        Frames to dataset.
        """
        self.dataset_df = self.__download()
        self.__add_wikipedia_title()
        self.__add_wikipedia_page()
        self.__add_relationships()
