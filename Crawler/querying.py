# Code for ranking documents based on query
import logging
import math
import re
import time

import hbase_util
import logger

from index_mgr_obj import index_mgr

RESULT_LIMIT = 100

# Get actual value from Hbase
corpus_sz = hbase_util.get_indexed_corpus_size()
print(corpus_sz)

def sanitize_query(query):
  """ Replace spacial charachters with spaces
  """
  pattern = re.compile('[\W_]+')
  query = pattern.sub(' ',query).lower()
  # TODO: Remove stop words and convert stemming
  return query


class Query:
  def __init__(query, and_query, not_query):
    self.or_words = query.split()
    self.and_words = and_query.split()
    self.not_words = not_query.split()
    self.words = self.or_words + self.and_words \
      + self.not_words

    self.word_dicts = {}

  def filter_docs(self):
    """ Filter docs based on boolean operators
    """
    word_dicts = self.word_dicts
    docs = set()

    for word in self.or_words:
      doc_set = word_dicts[word].keys()
      docs = docs.union(doc_set)

    for word in self.and_words:
      doc_set = word_dicts[word].keys()
      docs = docs.intersection(doc_set)

    for word in self.not_words:
      doc_set = word_dicts[word].keys()
      docs = docs - doc_set

    self.filtered_docs = docs

  # To be added
  def phrase_query(self, string):
    """
    Given query string - returns dict of docs containing the phrase
    with the scores added up and pos of the first term.
    Call this function instead of get_occuring_docs of hbase_util and
    treat the entire string as one term
    """
    result = {}
    pos_dict = {}
    i = 0

    string = sanitize_query(string)
    for word in string.split():
      if i == 0:
        result = hbase_util.get_occuring_docs(word)
        for doc in result.keys():
          pos_dict[doc] = set(result[doc]["pos"])
      else:
        temp_dict = hbase_util.get_occuring_docs(word)
        # check if doc exists in doc_dict of all other words
        for doc in result.keys():
          if doc not in temp_dict.keys():
            del result[doc]
          else:
            # check if position of ith word is i next to first word
            pos_dict[doc] = pos_dict[doc].intersection(
              [posn-i for posn in temp_dict[doc]["pos"]])
            if not pos_dict:
              del result[doc]
            else:
              # document has passed checks till now and scores are updated
              for score in result[doc]:
                result[doc][score] += temp_dict[doc][score]
      i += 1
    return result

  def rank(self, rank_name = "tfidf"):
    """ Return a list of ranked documents
    """
    for word in self.words:
      self.word_dicts[word] = get_occuring_docs(word)

    self.filter_docs()

    doc_list = list(self.filtered_docs)
    present_words = self.and_words + self.or_words

    score_list = []
    for doc in doc_list:
      doc_score = 0
      for word in present_words:
        if doc in self.word_dicts[word]:
          # TODO: Correct this score
          doc_score += self.word_dicts[word][doc]["tf1"]

      score_list.append((doc_score, doc))

    score_list = sorted(score_list)
    ranked_docs = [pair[1] for pair in score_list]

    return ranked_docs



def compute_tf_idf(term, results, operator="or"):
  doc_list = hbase_util.get_occuring_docs(term)
  no_docs = len(doc_list)
  for doc in doc_list:
    tf = len(doc_list[doc].split())
    if doc in results.keys():
      results[doc].append((tf, no_docs))
    else:
      results[doc] = [(tf, no_docs)]


def tf_idf1(query):
  """
  Calculates score of each doc as SUM over each term -
  term_freq(term)*log(size_corpus/no_of_docs_term_occurs_in) 
  """
  results = {}

  for term in query.split():
    compute_tf_idf(term, results)

  logging.info("TFIDF doc length: " + str(len(results)))
  results_rank = []
  # print(results)
  for doc in results.keys():
    score = 0
    for tf, no_docs in results[doc]:
      score += tf*(math.log(corpus_sz/no_docs))
    results_rank.append((score, doc))
  results_rank = sorted(results_rank, reverse=True)
  logging.info("Scores after ranking: %s" %results_rank[:10])
  return [doc for sc, doc in results_rank]


class Bm25Ranker:
  # bm parameters, can be adjusted
  bm_b = 0.75
  bm_k = 1.6
  avg_doc_len = 100

  # query needs to be the sanitized query
  def __init__(self, query, tfidf = None):
    self.words = query.split()
    self.tfidf = tfidf

  def preprocess(self):
    if not (self.tfidf is None):
      return
    self.tfidf = {}
    for term in self.words:
      compute_tf_idf(term, self.tfidf)

  def idf_bm(self, containing):
    return math.log((corpus_sz - containing + 0.5) \
        / (containing + 0.5))

  def bm25_term_score(self, doc, doc_len, tf, containing):
    return (self.idf_bm(containing) * ((tf * (Bm25Ranker.bm_k + 1)) \
        / (tf + (Bm25Ranker.bm_k * (1 - Bm25Ranker.bm_b +  \
          (Bm25Ranker.bm_b * (doc_len) / (Bm25Ranker.avg_doc_len)))))))

  def rank(self):
    t1 = time.time()
    self.preprocess()
    logging.info("BM25 TfIDf computation: " + str(time.time() - t1))
    t1 = time.time()
    tfidf = self.tfidf
    results = [] # Contains all doc scores
    logging.info("Len")

    logging.info("BM25 doc count: " + str(len(tfidf)))
    for doc in tfidf.keys():
      st = time.time()
      score = 0
      doc_len = hbase_util.get_doc_length(doc)
      # Doc score is the sum of term scores
      # for each query term
      for term in tfidf[doc]:
        score += self.bm25_term_score(doc, doc_len, term[0], term[1])
      results.append((score, doc))
      logging.info("Score per doc: " + str(time.time() - st))

    logging.info("Scores: " + str(time.time() - t1))
    # Sort by descending order of score and return 
    results = sorted(results, reverse = True)
    logging.info("Sort: " + str(time.time() - t1))
    return [doc for sc, doc in results]


def rank_results(query, rank_name="tfidf",
    and_query = "", not_query = ""):
  """ Return list of urls in ranked order
  """
  logger.initialize()
  t1 = time.time()

  if not (rank_name in rank_fn):
    logging.error("Unknown rank algorithm: %s" %rank_name)
    return []

  query = sanitize_query(query)
  and_query = sanitize_query(and_query)
  not_query = sanitize_query(not_query)

  results_rank = Query(query, and_query, not_query).rank(rank_name)[:RESULT_LIMIT]
  
  # print(results_rank[:10])
  b_url_list = hbase_util.get_url(results_rank)
  url_list = [b_url.decode("utf-8") for b_url in b_url_list]

  logging.info("Ranked the results for the query: %s in %f sec" %(query,
    time.time()-t1))
  
  return url_list

if __name__ == "__main__":
  query = input("Search: ")
  rank_results(query)