from pyspark import SparkContext, SparkConf
from pyspark.sql import SparkSession, SQLContext
import boto3
from botocore.config import Config
from warcio.archiveiterator import ArchiveIterator
from warcio.bufferedreaders import BytesIO
from selectolax.parser import HTMLParser
from bs4 import BeautifulSoup
import datetime
import re
from operator import add


# ==============================================================
# ==================== Deklaracje funkcji ======================
# ==============================================================

# Funkcja wczytująca i przetwarzająca pliki WARC z Common Crawl w celu zliczenia wystąpień słów kluczy
# w danym przedziale czasu.
# Funkcja zwraca klucz w postaci data + słowo klucz oraz ilość wystąpień danego słowa klucza
def process_warc_records(warc_records):
    s3_client = boto3.client('s3', region_name='us-east-1')

    for record in warc_records:
        warc_path = record['warc_filename']
        warc_offset = int(record['warc_record_offset'])
        warc_length = int(record['warc_record_length'])
        warc_range = 'bytes={}-{}'.format(warc_offset, (warc_offset+warc_length-1))
        # Wczytywanie pliku WARC z Common Crawl

        try:
            response = s3_client.get_object(Bucket='commoncrawl', Key=warc_path, Range=warc_range)

            # Odczytywanie danych z pliku WARC
            warc_record_stream = BytesIO(response["Body"].read())

            # Przetwarzanie rekordów pliku WARC
            for warc_record in ArchiveIterator(warc_record_stream):
                # Odczytywanie daty pliku WARC
                date_str = warc_record.rec_headers.get_header("WARC-Date").split("T")[0]
                date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                # Czytanie danych zapisanych w postaci HTML
                page = warc_record.content_stream().read()
                soup = BeautifulSoup(page, features="html.parser")
                # wyrzucenie elementów 'script' i 'style'
                for script in soup(["script", "style"]):
                    script.extract()
                # zamiana na czysty tekst
                page_text = soup.get_text()

                # Zliczanie wystąpień każdego słowa klucza w tekście
                counted_words = map(lambda w: w.lower(), word_pattern.findall(page_text))
                for word in counted_words:
                    key = str(date_obj) + '/' + word
                    yield key, 1
        except Exception as e:
            pass



# ==============================================================
# ==================== Początek programu =======================
# ==============================================================

print("==============================================")
print("+--------------------------------------------+")
print("==============================================")

# Zmienna definiująca gdzie ma być zapisany plik wynikowy(Bucket użytkownika na S3)
output_bucket_folder = 's3://ase7/dane.csv'

# Słowa klucze
key_words = ['covid', 'covid19', 'pandemia', 'wirus']
word_pattern = re.compile(r'\b(?:%s)\b' % '|'.join(key_words))

# Inicializacja Sparka
conf = (SparkConf()
        .setAppName("Ase"))

sc = SparkContext(conf=conf)
sql_context = SQLContext(sc)
spark = SparkSession.builder.getOrCreate()

# Wczytanie danych z bucketa Common Crawl
input_bucket = 's3://commoncrawl/cc-index/table/cc-main/warc/'
df = sql_context.read.load(input_bucket)
df.createOrReplaceTempView('ccindex')

# Użycie odpowiedniego zapytania SQL w celu stworzenia DataFrame, który zawiera spis plików WARC
# z interesującego nas przedziału danych
query_txt = 'SELECT warc_filename, warc_record_offset, warc_record_length FROM ccindex ' \
            'WHERE (crawl = "CC-MAIN-2019-13" OR crawl = "CC-MAIN-2019-18" OR crawl = "CC-MAIN-2020-16") ' \
            'AND subset = "warc" AND url_host_tld = "pl"'
query_txt_2019_03 = 'SELECT warc_filename, warc_record_offset, warc_record_length FROM ccindex ' \
            'WHERE crawl = "CC-MAIN-2019-13"' \
            'AND subset = "warc" AND url_host_tld = "pl"'
query_txt_2019_04 = 'SELECT warc_filename, warc_record_offset, warc_record_length FROM ccindex ' \
            'WHERE crawl = "CC-MAIN-2019-18"' \
            'AND subset = "warc" AND url_host_tld = "pl"'
query_txt_2020 = 'SELECT warc_filename, warc_record_offset, warc_record_length FROM ccindex ' \
            'WHERE crawl = "CC-MAIN-2020-16"' \
            'AND subset = "warc" AND url_host_tld = "pl"'
sqlDF = sql_context.sql(query_txt_2020)
# sqlDF.show()

# Odczytanie i przetwarzanie plików WARC(zliuczanie wystąpień słów kluczy w danym dniu)
word_counts = sqlDF.rdd.repartition(6000).mapPartitions(process_warc_records).reduceByKey(lambda a, b: a + b).collect()

# Sparsowanie wyników
word_counts_array = []
for row in word_counts:
    word_key = row[0].split("/")
    date = word_key[0]
    word = word_key[1]
    count = int(row[1])
    word_counts_array.append([date, word, count])

# Sortowanie wyników względem daty
word_counts_sorted = sorted(word_counts_array, key=lambda word_row: word_row[0])

# Stworzenie finalnej tablicy z danymi w celu łatwiejszej ich wizualizacji i
# przetwarzania(w każdym rzędzie mamy ilość wystąpień datę oraz ilość wystąpień słowa klucza tego dnia)
word_counts_final = []
index_row = ['DATE']
for word in key_words:
    index_row.append(word)

if len(word_counts_sorted) > 0:
    temp_row = [word_counts_sorted[0][0]]
    for i in range(len(key_words)):
        temp_row.append(0)
    word_counts_final.append(temp_row)

    for row in word_counts_sorted:
        items = len(word_counts_final)
        date = row[0]
        word = row[1]
        count = row[2]
        if word_counts_final[items - 1][0] != date:
            new_row = [date]
            for i in range(len(key_words)):
                new_row.append(0)
            word_counts_final.append(new_row)
            items = len(word_counts_final)
        for i in range(1, len(index_row), 1):
            key_word = index_row[i]
            if word == key_word:
                word_counts_final[items - 1][i] = count
                break

    # Zamiana finalnej tabeli na DataFrame i zapisanie danych do pliku .csv
    df_words = sc.parallelize(word_counts_final).repartition(1).sortByKey().toDF(index_row)
    df_words.show()
    df_words.write.csv(output_bucket_folder, mode='overwrite', header=True)


print("==============================================")
print("+--------------------------------------------+")
print("==============================================")
