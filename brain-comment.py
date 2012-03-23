# Copyright (C) 2011 Peter Teichman

import collections
import logging
import math
import os
import random
import re
import sqlite3
import time
import types

from .instatrace import Instatrace
from . import scoring
from . import tokenizers

log = logging.getLogger("cobe")

# use an empty string to denote the start/end of a chain
_END_TOKEN_TEXT = ""
_NEXT_TOKEN_TABLE = "next_token"
_PREV_TOKEN_TABLE = "prev_token"

_trace = Instatrace()


class Brain:
    """The main interface for Cobe."""
    def __init__(self, filename, instatrace=None):
        """Construct a brain for the specified filename. If that file
        doesn't exist, it will be initialized with the default brain
        settings."""
        if not os.path.exists(filename):
            log.info("File does not exist. Assuming defaults.")
            Brain.init(filename)

        if instatrace is not None:
            _trace.init(instatrace)

        _start = _trace.now()
        self._db = db = _Sql(sqlite3.connect(filename))
        _trace.trace("Brain.connect_us", _trace.now() - _start)

        self.order = int(db.get_info_text("order"))

        self.scorer = scoring.ScorerGroup()
        self.scorer.add_scorer(1.0, scoring.CobeScorer(self.order))

        tokenizer_name = db.get_info_text("tokenizer")
        if tokenizer_name == "MegaHAL":
            self.tokenizer = tokenizers.MegaHALTokenizer()
        else:
            self.tokenizer = tokenizers.CobeTokenizer()

        self.stemmer = None
        stemmer_name = db.get_info_text("stemmer")

        if stemmer_name is not None:
            try:
                self.stemmer = tokenizers.CobeStemmer(stemmer_name)
            except Exception, e:
                log.error("Error creating stemmer: %s", str(e))

        self._end_token_id = db.get_token_id(_END_TOKEN_TEXT)
        self._batch_learning = False

    def start_batch_learning(self):
        """Begin a series of batch learn operations. Data will not be
        committed to the database until stop_batch_learning is
        called. Learn text using the normal learn(text) method."""
        self._batch_learning = True

    def stop_batch_learning(self):
        """Finish a series of batch learn operations."""
        self._batch_learning = False
        self._db.commit()

    def del_stemmer(self):
        self.stemmer = None

        self._db.delete_token_stems()

        self._db.set_info_text("stemmer", None)
        self._db.commit()

    def set_stemmer(self, language):
        self.stemmer = tokenizers.CobeStemmer(language)

        self._db.delete_token_stems()
        self._db.update_token_stems(self.stemmer)

        self._db.set_info_text("stemmer", language)
        self._db.commit()

    def learn(self, text):
        """Learn a string of text. If the input is not already
        Unicode, it will be decoded as utf-8."""
        if type(text) != types.UnicodeType:
            # Assume that non-Unicode text is encoded as utf-8, which
            # should be somewhat safe in the modern world.
            text = text.decode("utf-8", "ignore")

        tokens = self.tokenizer.split(text)
        _trace.trace("Brain.learn_input_token_count", len(tokens))

        if len(tokens) < self.order:
            log.debug("Input too short to learn: %s", text)
            return
        # what is learning? lt is token count, expr count, expr-token count
        self._learn_tokens(tokens)

    def _learn_tokens(self, tokens):
        db = self._db
        c = db.cursor()

        token_ids = self._get_or_register_tokens(tokens, c)
        n_exprs = len(token_ids) - self.order

        # increment seen count for each token
        db.inc_token_counts(token_ids, c=c)

        links = []

        for i in xrange(n_exprs + 1):
            expr = token_ids[i:i + self.order]
            expr_id = self._get_or_register_expr(expr, c=c)

            # increment the expr count
            db.inc_expr_count(expr_id, c=c)

            if i == 0:
                # add link to boundary on prev_token
                # _end_token_id is another name of setence seperator
                # so, first expression has a PREV named end_token.
                # this table is used to specify which token is before the expr
                links.append((_PREV_TOKEN_TABLE, expr_id, self._end_token_id))

            if i > 0:
                # link prev token to this expr
                prev_token = token_ids[i - 1]
                links.append((_PREV_TOKEN_TABLE, expr_id, prev_token))

            if i < n_exprs:
                # link next token to this expr
                next_token = token_ids[i + self.order]
                links.append((_NEXT_TOKEN_TABLE, expr_id, next_token))

            if i == n_exprs:
                # add link to boundary on next_token
                links.append((_NEXT_TOKEN_TABLE, expr_id, self._end_token_id))

        if len(links) > 0:
            db.add_or_inc_links(links, c=c)

        if not self._batch_learning:
            db.commit()

    def reply(self, text):
        """Reply to a string of text. If the input is not already
        Unicode, it will be decoded as utf-8."""
        # no learning in this function!
        if type(text) != types.UnicodeType:
            # Assume that non-Unicode text is encoded as utf-8, which
            # should be somewhat safe in the modern world.
            text = text.decode("utf-8", "ignore")

        # each reply gets its own database cache for now
        db_cache = DbCache(self._db)

        # usually application only use Cache, not directed in DB.
        tokens = self.tokenizer.split(text)
        input_ids = db_cache.get_token_ids(tokens)

        # filter out unknown words and non-words from the potential pivots
        # any token cannot find in db, or is not a word will be filtered.
        pivot_set = self._filter_pivots(input_ids, db_cache)

        # Conflate the known ids with the stems of their words
        if self.stemmer is not None:
            self._conflate_stems(pivot_set, tokens)

        # If we didn't recognize any word tokens in the input, pick
        # something random from the database and babble.
        if len(pivot_set) == 0:
            pivot_set = self._babble()

        if len(pivot_set) == 0:
            # we couldn't find any pivot words in _babble(), so we're
            # working with an essentially empty brain. Use the classic
            # MegaHAL reply:
            return "I don't know enough to answer you yet!"

        best_score = -1.0
        best_reply = None

        # loop for half a second
        start = time.time()
        end = start + 0.5
        count = 0

        all_replies = []

        _start = _trace.now()
        # it just try half a second to get a list of replies. and Choose the best
        while best_reply is None or time.time() < end:
            _now = _trace.now()
            reply = self._generate_reply(pivot_set, db_cache)
            _trace.trace("Brain.generate_reply_us", _trace.now() - _now)

            if reply is None:
                continue

            token_ids, pivot_idx = reply

            _now = _trace.now()
            score = self._evaluate_reply(input_ids, token_ids, db_cache)

            log.info("_generate_reply score %f : %s " % (score, \
            "|".join(db_cache.get_token_texts(token_ids))))


            _trace.trace("Brain.evaluate_reply_us", _trace.now() - _now)

            _trace.trace("Brain.reply_output_token_count", len(token_ids))

            count += 1

            if score > best_score:
                best_score = score
                best_reply = token_ids

            # dump all replies to the console if debugging is enabled
            if log.isEnabledFor(logging.DEBUG):
                all_replies.append((score, token_ids, pivot_idx))

        all_replies.sort()
        for score, token_ids, pivot_idx in all_replies:
            words = db_cache.get_token_texts(token_ids)
            words[pivot_idx] = "[%s]" % words[pivot_idx]

            text = self.tokenizer.join(words)
            log.debug("%f %s", score, text.encode("utf-8"))

        _trace.trace("Brain.reply_input_token_count", len(tokens))
        _trace.trace("Brain.known_word_token_count", len(pivot_set))

        _time = _trace.now() - _start
        _trace.trace("Brain.reply_us", _time)
        _trace.trace("Brain.reply_count", count, _time)
        _trace.trace("Brain.best_reply_score", int(best_score * 1000))
        _trace.trace("Brain.best_reply_length", len(best_reply))
        log.debug("made %d replies in %f seconds" % (count,
                                                     time.time() - start))

        # look up the words for these tokens
        _now = _trace.now()
        text = db_cache.get_token_texts(best_reply)
        _trace.trace("Brain.reply_words_lookup_us", _trace.now() - _now)

        return self.tokenizer.join(text)

    def _conflate_stems(self, pivot_set, tokens):
        for token in tokens:
            # stem make one word to several words. use the tuple of (w1, w2, ...)
            # to replace the tokens of w1, w2, ...
            stem_ids = self._db.get_token_stem_ids(self.stemmer.stem(token))
            if len(stem_ids) == 0:
                continue

            # add the tuple of stems to the pivot set, and then
            # remove the individual token_ids
            pivot_set.add(stem_ids)

            for stem_id in stem_ids:
                try:
                    pivot_set.remove(stem_id)
                except KeyError:
                    pass

    def _babble(self):
        # random generate length of 5
        token_ids = []
        for i in xrange(5):
            # Generate a few random tokens that can be used as pivots
            token_id = self._db.get_random_word_token()

            if token_id is not None:
                token_ids.append(token_id)

        return token_ids

    def _filter_pivots(self, pivot_set, db_cache):
        # remove pivots that might not give good results
        # current, only check whether the token is word.
        filtered = set()

        for pivot_id in pivot_set:
            if pivot_id is not None and db_cache.get_token_is_word(pivot_id):
                filtered.add(pivot_id)

        return filtered

    def _choose_pivot(self, pivot_ids):
        # just random select. if selected tuple, select again
        pivot = random.choice(tuple(pivot_ids))

        if type(pivot) is types.TupleType:
            # the input word was stemmed to several things
            pivot = random.choice(pivot)

        return pivot

    def _generate_reply(self, token_probs, db_cache):
        if len(token_probs) == 0:
            return

        # generate a reply containing one of token_ids
        token_id = self._choose_pivot(token_probs)
        # get the expr index and the token positon in the expr
        expr_id, expr_idx = self._db.get_random_expr(token_id)

        if expr_id is None:
            return

        # for the expr, run forward, and run backward
        next_token_ids = db_cache.follow_chain(_NEXT_TOKEN_TABLE, expr_id)
        prev_token_ids = db_cache.follow_chain(_PREV_TOKEN_TABLE, expr_id)

        # Save the index of the pivot token in the reply.
        # pivot is like weight point of the sentence. len(prev_token_ids) - order
        # is the extention number in prev. If it is add,. pivot should add same from expr_idx
        # so the selected word will not change (in rely)
        pivot_idx = len(prev_token_ids) - self.order + expr_idx

        log.info("follow_chain RESULT-PREV : %s " % ( \
            "|".join(db_cache.get_token_texts(prev_token_ids))))

        log.info("follow_chain RESULT-NEXT : %s " % ( \
            "|".join(db_cache.get_token_texts(next_token_ids))))

        # strip the original expr from the prev reply
        # can also strip next reply (they both contain original expr)
        for i in xrange(self.order):
            prev_token_ids.pop()

        reply = list(prev_token_ids)
        reply.extend(next_token_ids)

        return reply, pivot_idx

    def _evaluate_reply(self, input_tokens, output_tokens, db_cache):
        score_memo = db_cache.cache.setdefault("score_memo", {})

        # use hash(tuple()) to reduce output_tokens to an integer for storage
        # no need to score same ouput_tokens, if same, the later will give -1
        reply_key = hash(tuple(output_tokens))
        if reply_key in score_memo:
            return -1.0

        score = self.scorer.score(input_tokens, output_tokens, db_cache)
        score_memo[reply_key] = score
        return score

    def _get_or_register_tokens(self, tokens, c):
        token_ids = []
        # wul:  memo has no use. may delete
        memo = {}
        for token in tokens:
            token_id = self._db.get_token_id(token)
            # first add this token(word)
            if token_id is None:
                # it can decide whether it is English word. but for others?
                if re.search("\w", token, re.UNICODE):
                    is_word = True
                else:
                    is_word = False

                token_id = self._db.insert_token(token, is_word, c=c)
                # if stemmer is present, store stem
                if is_word and self.stemmer is not None:
                    self._db.insert_stem(token_id, self.stemmer.stem(token))

                memo[token] = token_id

            token_ids.append(token_id)

        return token_ids

    def _get_or_register_expr(self, token_ids, c):
        expr_id = self._db.get_expr_by_token_ids(token_ids, c=c)

        if expr_id is None:
            expr_id = self._db.insert_expr(token_ids, c=c)

        return expr_id

    @staticmethod
    def init(filename, order=5, tokenizer=None):
        """Initialize a brain. This brain's file must not already exist.

Keyword arguments:
order -- Order of the forward/reverse Markov chains (integer)
tokenizer -- One of Cobe, MegaHAL (default Cobe). See documentation
             for cobe.tokenizers for details. (string)"""
        log.info("Initializing a cobe brain: %s" % filename)

        if tokenizer is None:
            tokenizer = "Cobe"

        if tokenizer not in ("Cobe", "MegaHAL"):
            log.info("Unknown tokenizer: %s. Using CobeTokenizer", tokenizer)
            tokenizer = "Cobe"

        db = _Sql(sqlite3.connect(filename))

        _now = _trace.now()
        db.init(order, tokenizer)
        _trace.trace("Brain.init_time_us", _trace.now() - _now)


class DbCache:
    """A class to memoize database functions within the context of one reply"""
    # cache is just a dictionary
    def __init__(self, db):
        self.db = db
        self.cache = {}

    def get_token_id(self, word):
        # get handle of token_id table
        memo = self.cache.setdefault("token_id", {})

        # try to get token id for a word from cache. if missed, use db to get
        try:
            token_id = memo[word]
        except KeyError:
            token_id = self.db.get_token_id(word)
            memo[word] = token_id

        return token_id

    def get_token_ids(self, words):
        return [self.get_token_id(word) for word in words]

    def get_token_info(self, token_id):
        # get handle of token_info table
        memo = self.cache.setdefault("token_info", {})

        try:
            info = memo[token_id]
        except KeyError:
            info = self.db.get_token_info(token_id)
            memo[token_id] = info

        return info

    def get_token_text(self, token_id):
        return self.get_token_info(token_id)["text"]

    def get_token_texts(self, token_ids):
        return [self.get_token_text(token_id) for token_id in token_ids]

    def get_token_is_word(self, token_id):
        return self.get_token_info(token_id)["is_word"]

    def get_expr_token_ids(self, expr_id):
        memo = self.cache.setdefault("expr_token_ids", {})

        try:
            token_ids = memo[expr_id]
        except KeyError:
            token_ids = tuple(self.db.get_expr_token_ids(expr_id))
            memo[expr_id] = token_ids

        return token_ids
    def get_expr_token_ids(self, expr_id):
        memo = self.cache.setdefault("expr_token_ids", {})

        try:
            token_ids = memo[expr_id]
        except KeyError:
            token_ids = tuple(self.db.get_expr_token_ids(expr_id))
            memo[expr_id] = token_ids

        return token_ids
    def get_expr_token_probability(self, table, expr, token_id):
        memo = self.cache.setdefault("%s_p" % table, {})

        # FIXME: it's possible we could save some DB action by looking
        # up the expr_id for this expr first?

        key = (tuple(expr), token_id)
        try:
            p = memo[key]
        except KeyError:
            p = self.db.get_expr_token_probability(table, expr, token_id)
            memo[key] = p

        return p


    def follow_chain(self, table, expr_id):
        expr = self.get_expr_token_ids(expr_id)

        # initialize the chain with the current expr's tokens
        chain = collections.deque(expr)

        if table == _NEXT_TOKEN_TABLE:
            append = chain.append
            query = self.db.next_chain_q
            tablename = 'NEXT'
        else:
            append = chain.appendleft
            query = self.db.prev_chain_q
            tablename = 'PREV'

        log.info("follow_chain %s INIT : %s " % (tablename, \
            "|".join(self.get_token_texts(expr))))

        c = self.db.cursor()

        while True:
            # get the token
            # if is _NEXT_TOKEN_TABLE, each time get a new expr and new
            # token , the new token should be last token in the expr.
            # the new token select is randomed to be anyone next to previous expr.
            c.execute(query, {"expr_id": expr_id})

            row = c.fetchone()
            if not row or row[0] == self.db._end_token_id:
                break

            next_token_id, expr_id = row
            log.info("follow_chain %s GET %s in %s " % (tablename, self.get_token_text(next_token_id) , \
            "|".join(self.get_token_texts(self.get_expr_token_ids(expr_id)))))
            append(next_token_id)
        # HEY, the chain contain the basic (input) expr, but extend somewhat.
        # PREV extend to left side (so each new add word should can be sentence head)
        # NEXT extend to right side (so each new add word shoud can be sentence end)
        return chain


class _Sql:
    """Database functions to support a Cobe brain. This is not meant
    to be used from outside."""
    def __init__(self, conn, run_migrations=True):
        # conn is sql3 object(connection)
        self._conn = conn
        # set format of  row connection fetching
        conn.row_factory = sqlite3.Row

        # the flag of "inited" is "order(n-gram)" has been set in db
        if self.is_initted():
            if run_migrations:
                self._run_migrations()
            # record some fast link
            self._order = int(self.get_info_text("order"))
            self._end_token_id = self.get_token_id(_END_TOKEN_TEXT)

            self._all_tokens = ",".join(["token%d_id" % i
                                         for i in xrange(self._order)])
            self._all_token_args = " AND ".join(["token%d_id = ?" % i
                                                 for i in xrange(self._order)])
            self._all_token_q = ",".join(["?" for i in xrange(self._order)])

            # construct to check the relation of two expressions(by compare tokes)
            # construct partial subqueries for use when following chains
            next_parts = []
            prev_parts = []
            for i in xrange(self._order - 1):
                next_parts.append("next_expr.token%d_id = expr.token%d_id" %
                                  (i, i + 1))
                prev_parts.append("prev_expr.token%d_id = expr.token%d_id" %
                                  (i + 1, i))
            next_query = " AND ".join(next_parts)
            prev_query = " AND ".join(prev_parts)
            # construct sql, combined select of table expr with itself (named as next_expr)
            # :expr_id show it is a parameter of sql3 connection.
            self.next_chain_q = \
                "SELECT next_expr.token%(last_token)d_id, next_expr.id FROM expr, expr AS next_expr WHERE expr.id = :expr_id AND next_expr.token%(last_token)d_id = (SELECT token_id FROM %(table)s WHERE expr_id = :expr_id LIMIT 1 OFFSET ifnull(random()%%(SELECT count(*) FROM %(table)s WHERE expr_id = :expr_id), 0)) AND %(subquery)s" \
                % {"last_token": self._order - 1,
                   "subquery": next_query,
                   "table": _NEXT_TOKEN_TABLE}

            self.prev_chain_q = "SELECT prev_expr.token0_id, prev_expr.id FROM expr, expr AS prev_expr WHERE expr.id = :expr_id AND prev_expr.token0_id = (SELECT token_id FROM %(table)s WHERE expr_id = :expr_id LIMIT 1 OFFSET ifnull(random()%%(SELECT count(*) FROM %(table)s WHERE expr_id = :expr_id), 0)) AND %(subquery)s" \
                % {"subquery": prev_query,
                   "table": _PREV_TOKEN_TABLE}

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        _start = _trace.now()
        ret = self._conn.commit()
        _trace.trace("Brain.db_commit_us", _trace.now() - _start)
        return ret

    def close(self):
        return self._conn.close()

    def is_initted(self, c=None):
        if c is None:
            c = self.cursor()

        try:
            self.get_info_text("order")
            return True
        except sqlite3.OperationalError:
            return False

    def set_info_text(self, attribute, text, c=None):
        if c is None:
            c = self.cursor()

        if text is None:
            q = "DELETE FROM info WHERE attribute = ?"
            c.execute(q, (attribute,))
        else:
            q = "UPDATE info SET text = ? WHERE attribute = ?"
            c.execute(q, (text, attribute))

            if c.rowcount == 0:
                q = "INSERT INTO info (attribute, text) VALUES (?, ?)"
                c.execute(q, (attribute, text))

    def get_info_text(self, attribute, default=None, text_factory=None, c=None):
        if c is None:
            c = self.cursor()

        if text_factory is not None:
            old_text_factory = self._conn.text_factory
            self._conn.text_factory = text_factory

        q = "SELECT text FROM info WHERE attribute = ?"
        row = c.execute(q, (attribute,)).fetchone()

        if text_factory is not None:
            self._conn.text_factory = old_text_factory

        if row:
            return row[0]

        return default

    def get_token_id(self, token, c=None):
        if c is None:
            c = self.cursor()

        q = "SELECT id FROM tokens WHERE text = ?"
        row = c.execute(q, (token,)).fetchone()
        if row:
            return int(row[0])

    def get_token_stem_ids(self, stem, c=None):
        if c is None:
            c = self.cursor()

        q = "SELECT token_id FROM token_stems WHERE token_stems.stem = ?"
        rows = c.execute(q, (stem,))
        if rows:
            return tuple(val[0] for val in rows)

    def get_random_word_token(self, c=None):
        if c is None:
            c = self.cursor()

        # select a random row from tokens
        q = "SELECT id FROM tokens WHERE is_word = 1 AND id >= abs(random()) % (SELECT MAX(id) FROM tokens) + 1"
        row = c.execute(q).fetchone()

        if row:
            return row["id"]

    def get_token_info(self, token_id, c=None):
        if c is None:
            c = self.cursor()

        q = "SELECT text, is_word FROM tokens WHERE id = ?"
        row = c.execute(q, (token_id,)).fetchone()
        if row:
            return row

    def get_expr_token_ids(self, expr_id, c=None):
        if c is None:
            c = self.cursor()

        q = "SELECT %s FROM expr WHERE id = ?" % self._all_tokens
        return c.execute(q, (expr_id,)).fetchone()

    def insert_token(self, token, is_word, c=None):
        if c is None:
            c = self.cursor()

        q = "INSERT INTO tokens (text, is_word, count) VALUES (?, ?, 0)"
        c.execute(q, (token, is_word))

        return c.lastrowid

    def insert_stem(self, token_id, stem, c=None):
        if c is None:
            c = self.cursor()

        q = "INSERT INTO token_stems (token_id, stem) VALUES (?, ?)"
        c.execute(q, (token_id, stem))

    def insert_expr(self, token_ids, c=None):
        if c is None:
            c = self.cursor()

        q = "INSERT INTO expr (count,%s) VALUES (0,%s)" % (self._all_tokens,
                                                           self._all_token_q)

        c.execute(q, token_ids)
        return c.lastrowid

    def inc_token_counts(self, token_ids, c=None):
        if c is None:
            c = self.cursor()

        q = "UPDATE tokens SET count = count + 1 WHERE id = ?"
        for token_id in token_ids:
            c.execute(q, (token_id,))

    def inc_expr_count(self, expr_id, c=None):
        if c is None:
            c = self.cursor()

        q = "UPDATE expr SET count = count + 1 WHERE id = ?"
        c.execute(q, (expr_id,))

    def add_or_inc_links(self, links, c=None):
        if c is None:
            c = self.cursor()

        for (table, expr_id, token_id) in links:
            update_q = "UPDATE %s SET count = count + 1 WHERE expr_id = ? AND token_id = ?" % table
            c.execute(update_q, (expr_id, token_id))

            if c.rowcount == 0:
                insert_q = "INSERT INTO %s (expr_id, token_id, count) VALUES (?, ?, ?)" % table
                c.execute(insert_q, (expr_id, token_id, 1))

    def get_random_expr(self, token_id, c=None):
        if c is None:
            c = self.cursor()

        # try looking for the token in a random spot in the exprs
        positions = range(self._order)
        # make the order mess
        random.shuffle(positions)
        # get a expression which contain the token in a random position.
        for pos in positions:
            q = "SELECT id FROM expr WHERE token%d_id = ? LIMIT 1 OFFSET ifnull(abs(random())%%(SELECT count(*) from expr WHERE token%d_id = ?), 0)" \
                % (pos, pos)

            row = c.execute(q, (token_id, token_id)).fetchone()
            if row:
                return int(row[0]), pos

        return None, None

    def get_expr_by_token_ids(self, token_ids, c):
        q = "SELECT id FROM expr WHERE %s" % self._all_token_args

        row = c.execute(q, token_ids).fetchone()
        if row:
            return int(row[0])

    def _get_expr_token_count(self, table, expr_id, token_id, c):
        q = "SELECT count FROM %s WHERE expr_id = ? AND token_id = ?" % table

        row = c.execute(q, (expr_id, token_id)).fetchone()
        if row:
            return int(row[0])

    def get_expr_token_probability(self, table, expr, token_id, c=None):
        if c is None:
            c = self.cursor()

        expr_id, expr_count = self._get_expr_and_count_by_token_ids(expr, c)
        token_count = self._get_expr_token_count(table, expr_id, token_id, c)

        if token_count is None:
            return 0.

        return float(token_count) / float(expr_count)

    def _get_expr_and_count_by_token_ids(self, token_ids, c):
        q = "SELECT id, count FROM expr WHERE %s" % self._all_token_args

        row = c.execute(q, token_ids).fetchone()
        if row:
            return int(row[0]), int(row[1])

    def _get_random_next_token(self, table, expr_id, c):
        q = "SELECT token_id FROM %s WHERE expr_id = ? LIMIT 1 OFFSET ifnull(abs(random())%%(SELECT count(*) FROM %s WHERE expr_id = ?), 0)" % (table, table)

        c.execute(q, (expr_id, expr_id))

        row = c.fetchone()
        if row:
            return row[0]

    def init(self, order, tokenizer, run_migrations=True):
        c = self.cursor()
        # generate table for config
        log.debug("Creating table: info")
        c.execute("""
CREATE TABLE info (
    attribute TEXT NOT NULL PRIMARY KEY,
    text TEXT NOT NULL)""")
        # generate talbe for tokens. a token a unique slice of text.which is one world
        # or several words
        log.debug("Creating table: tokens")
        c.execute("""
CREATE TABLE tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT UNIQUE NOT NULL,
    is_word INTEGER NOT NULL)""")
        # prepare a part of sql. REFERENCES means the filed of "token1_id"
        # "token2_id"etc, use value from filed "id" of table "token"
        tokens = []
        for i in xrange(order):
            tokens.append("token%d_id INTEGER NOT NULL REFERENCES token(id)" % i)

        # generate table for expressions, a express is consist of 5 (order) tokens
        log.debug("Creating table: expr")
        c.execute("""
CREATE TABLE expr (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    count INTEGER NOT NULL,
    %s)""" % ',\n    '.join(tokens))

        # generate table to count P(token| expr), token and expr should in corresponding
        # tables
        log.debug("Creating table: next_token")
        c.execute("""
CREATE TABLE next_token (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expr_id INTEGER NOT NULL REFERENCES expr (id),
    token_id INTEGER NOT NULL REFERENCES token (id),
    count INTEGER NOT NULL)""")
        # generate talbe to count P(token | expr), from right to left
        log.debug("Creating table: prev_token")
        c.execute("""
CREATE TABLE prev_token (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expr_id INTEGER NOT NULL REFERENCES expr (id),
    token_id INTEGER NOT NULL REFERENCES token (id),
    count INTEGER NOT NULL)""")

        if run_migrations:
            self._run_migrations()

        # write first token (END) in table token
        # create a token for the end of a chain
        self.insert_token(_END_TOKEN_TEXT, 0, c=c)

        # write some information in table info
        # save the order of this brain
        self.set_info_text("order", str(order), c=c)

        # save the tokenizer
        self.set_info_text("tokenizer", tokenizer)

        # save the brain/schema version
        self.set_info_text("version", "1")

        # create index on "text" field of "token" table
        # (some SQL server not support TEXT as primary key or index)
        c.execute("""
CREATE INDEX tokens_text on tokens (text)""")

        # create index for each column(token) of table expr
        for i in xrange(order):
            c.execute("""
CREATE INDEX expr_token%d_id on expr (token%d_id)""" % (i, i))
        # create index for full-columns(tokens) of table expr
        token_ids = ",".join(["token%d_id" % i for i in xrange(order)])
        c.execute("""
CREATE INDEX expr_token_ids on expr (%s)""" % token_ids)
        # create combined index of (expr, token) of table next_token
        c.execute("""
CREATE INDEX next_token_expr_id ON next_token (expr_id, token_id)""")
        # create combined index of (expr, token) of table prev_token
        c.execute("""
CREATE INDEX prev_token_expr_id ON prev_token (expr_id, token_id)""")

        self.commit()
        c.close()

        self.close()

    def delete_token_stems(self):
        c = self.cursor()

        try:
            c.execute("""
DROP INDEX token_stems_stem""")
        except sqlite3.OperationalError:  # no such index: tokens_stems_stem
            pass

        try:
            c.execute("""
DROP INDEX token_stems_id""")
        except sqlite3.OperationalError:  # no such index: tokens_stems_id
            pass

        # delete all the existing stems from the table
        c.execute("""
DELETE FROM token_stems""")

        self.commit()

    def update_token_stems(self, stemmer):
        # stemmer is a CobeStemmer
        _start = _trace.now_ms()

        c = self.cursor()
        # select all the text in "tokens" table, which is word
        q = c.execute("""
SELECT id, text FROM tokens WHERE is_word = 1""")

        insert_q = "INSERT INTO token_stems (token_id, stem) VALUES (?, ?)"
        insert_c = self.cursor()
        # add item in "stem" talbe for token to its "stemmed text" mapping
        for row in q:
            insert_c.execute(insert_q, (row[0], stemmer.stem(row[1])))

        self.commit()

        _trace.trace("Db.update_token_stems_us", _trace.now_ms() - _start)

        _start = _trace.now_ms()
        # also create index of token, and index of stem
        c.execute("""
CREATE INDEX token_stems_id on token_stems (token_id)""")
        c.execute("""
CREATE INDEX token_stems_stem on token_stems (stem)""")
        _trace.trace("Db.index_token_stems_us", _trace.now_ms() - _start)

    def _run_migrations(self):
        _start = _trace.now()
        self._maybe_add_token_counts()
        self._maybe_add_token_stems()
        _trace.trace("Db.run_migrations_us", _trace.now() - _start)

    def _maybe_add_token_counts(self):
        c = self.cursor()
        # try to get one item. just testing column count exists
        # if not exist it will try to add a colummn of "count" in "tokens" table
        try:
            c.execute("""
SELECT count FROM tokens LIMIT 1""")
        except sqlite3.OperationalError:  # no such column: count
            self._add_token_counts(c)

        c.close()

    def _add_token_counts(self, c):
        log.info("SCHEMA UPDATE: adding token counts")
        _start = _trace.now_ms()

        # add a column in talbe "token", which by default has no "count"
        c.execute("""
ALTER TABLE tokens ADD COLUMN count INTEGER""")

        read_c = self.cursor()

        log.info("extracting next token counts")

        # select a temp table from "next_token", and get count of each token
        q = read_c.execute("""
SELECT count(*) AS count, token_id AS id FROM next_token GROUP BY id""")

        # update filed of "count" in "tokens" table according to temp table
        for row in q:
            c.execute("""
UPDATE tokens SET count = ? WHERE id = ?""", (row[0], row[1]))

        self.commit()

        log.info("extracting prev token counts")

        # update field of "count" in "tokens" table, it only update the NULL item
        # in "tokens" table, so it use a joint select which may be slow
        # add counts for tokens that were never in next_token
        q = read_c.execute("""
SELECT count(*) AS count, token_id AS id FROM prev_token,tokens WHERE tokens.count IS NULL AND tokens.id = prev_token.token_id GROUP BY id""")

        # it has same effect as (row[0], row[1])
        for row in q:
            c.execute("""
UPDATE tokens SET count = ? WHERE id = ?""", tuple(row))

        # Some tokens can still have NULL counts, if they have only been
        # found in training input shorter than the current Markov order.
        # Set their counts to 1 to have valid data.
        # set counts to 1 if no clue in "next-token" or "prev-token" table
        c.execute("""
UPDATE tokens SET count = 1 WHERE count IS NULL""")

        self.commit()
        _trace.trace("Db.add_token_counts_us", _trace.now_ms() - _start)

    def _maybe_add_token_stems(self):
        c = self.cursor()
        # only get one item, it is a test whether the table exists.
        # without commit and sql3 object will find the error
        try:
            c.execute("""
SELECT stem FROM token_stems LIMIT 1""")
        except sqlite3.OperationalError:  # no such table: token_stems
            self._add_token_stems(c)

        c.close()

    def _add_token_stems(self, c):
        log.info("SCHEMA UPDATE: adding token stems")
        _start = _trace.now_ms()
        # Create the token id to stem text mapping.
        c.execute("""
CREATE TABLE token_stems (
    token_id INTEGER,
    stem TEXT NOT NULL)""")

        self.commit()
        # it seems something missing. the content should be add.
        _trace.trace("Db.add_token_stems_us", _trace.now_ms() - _start)
