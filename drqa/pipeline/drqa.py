#!/usr/bin/env python3
# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""Full DrQA pipeline."""

import torch
import regex
import heapq
import math
import time
import logging

from multiprocessing import Pool as ProcessPool
from multiprocessing.util import Finalize

from tqdm import tqdm

from ..reader.vector import batchify, batchify_transformer , BERT_TOKENIZER
from ..reader.data import ReaderDataset, SortedBatchSampler
from .. import reader
from .. import tokenizers
from . import DEFAULTS

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Multiprocessing functions to fetch and tokenize text
# ------------------------------------------------------------------------------

PROCESS_TOK = None
PROCESS_DB = None
PROCESS_CANDS = None


def init(tokenizer_class, tokenizer_args, tokenizer_opts, db_class, db_opts, candidates=None):
    global PROCESS_TOK, PROCESS_DB, PROCESS_CANDS
    if tokenizer_args is None:
        PROCESS_TOK = tokenizer_class(**tokenizer_opts)
    else:
        PROCESS_TOK = tokenizer_class(*tokenizer_args, **tokenizer_opts)
    if hasattr(PROCESS_TOK, 'shutdown'):
        Finalize(PROCESS_TOK, PROCESS_TOK.shutdown, exitpriority=100)
    PROCESS_DB = db_class(**db_opts)
    Finalize(PROCESS_DB, PROCESS_DB.close, exitpriority=100)
    PROCESS_CANDS = candidates


def fetch_text(doc_id):
    global PROCESS_DB
    return PROCESS_DB.get_doc_text(doc_id)


def tokenize_text(text):
    return PROCESS_TOK.tokenize(text)


# ------------------------------------------------------------------------------
# Main DrQA pipeline
# ------------------------------------------------------------------------------


class DrQA(object):
    # Target size for squashing short paragraphs together.
    # 0 = read every paragraph independently
    # infty = read all paragraphs together
    GROUP_LENGTH = 0

    def __init__(
            self,
            reader_model=None,
            embedding_file=None,
            tokenizer=None,
            fixed_candidates=None,
            batch_size=32,
            cuda=True,
            data_parallel=False,
            max_loaders=5,
            num_workers=None,
            db_config=None,
            ranker_config=None
    ):
        """Initialize the pipeline.

        Args:
            reader_model: model file from which to load the DocReader.
            embedding_file: if given, will expand DocReader dictionary to use
              all available pretrained embeddings.
            tokenizer: string option to specify tokenizer used on docs.
            fixed_candidates: if given, all predictions will be constrated to
              the set of candidates contained in the file. One entry per line.
            batch_size: batch size when processing paragraphs.
            cuda: whether to use the gpu.
            data_parallel: whether to use multile gpus.
            max_loaders: max number of async data loading workers when reading.
              (default is fine).
            num_workers: number of parallel CPU processes to use for tokenizing
              and post processing resuls.
            db_config: config for doc db.
            ranker_config: config for ranker.
        """
        self.batch_size = batch_size
        self.max_loaders = max_loaders
        self.fixed_candidates = fixed_candidates is not None
        self.cuda = cuda

        logger.info('Initializing document ranker...')
        ranker_config = ranker_config or {}
        ranker_class = ranker_config.get('class', DEFAULTS['ranker'])
        ranker_opts = ranker_config.get('options', {})
        self.ranker = ranker_class(**ranker_opts)

        logger.info('Initializing document reader...')
        reader_model = reader_model or DEFAULTS['reader_model']
        self.reader_model = reader_model
        self.reader = reader.DocReader.load(reader_model, normalize=False)
        if embedding_file:
            logger.info('Expanding dictionary...')
            words = reader.utils.index_embedding_words(embedding_file)
            added = self.reader.expand_dictionary(words)
            self.reader.load_embeddings(added, embedding_file)
        if cuda:
            self.reader.cuda()
        if data_parallel:
            self.reader.parallelize()

        if not tokenizer:
            if reader_model != 'transformer':
                tok_class = DEFAULTS['tokenizer']
            else:
                from transformers import BertTokenizer
                tok_class = BertTokenizer.from_pretrained
        else:
            if reader_model != 'transformer':
                tok_class = tokenizers.get_class(tokenizer)
            else:
                from transformers import BertTokenizer
                tok_class = BertTokenizer.from_pretrained

        if reader_model != 'transformer':
            annotators = tokenizers.get_annotators_for_model(self.reader)
            tok_opts = {'annotators': annotators}
            tok_args = None
        else:
            tok_opts = {}
            tok_args = ('bert-base-uncased',)

        # ElasticSearch is also used as backend if used as ranker
        if hasattr(self.ranker, 'es'):
            db_config = ranker_config
            db_class = ranker_class
            db_opts = ranker_opts
        else:
            db_config = db_config or {}
            db_class = db_config.get('class', DEFAULTS['db'])
            db_opts = db_config.get('options', {})

        logger.info('Initializing tokenizers and document retrievers...')
        self.num_workers = num_workers
        self.processes = ProcessPool(
            num_workers,
            initializer=init,
            initargs=(tok_class, tok_args, tok_opts, db_class, db_opts, fixed_candidates)
        )

    def _split_doc(self, doc):
        """Given a doc, split it into chunks (by paragraph)."""
        curr = []
        curr_len = 0
        for split in regex.split(r'\n+', doc):
            split = split.strip()
            if len(split) == 0:
                continue
            # Maybe group paragraphs together until we hit a length limit
            if len(curr) > 0 and curr_len + len(split) > self.GROUP_LENGTH:
                yield ' '.join(curr)
                curr = []
                curr_len = 0
            curr.append(split)
            curr_len += len(split)
        if len(curr) > 0:
            yield ' '.join(curr)

    def _get_loader(self, data, num_loaders):
        """Return a pytorch data iterator for provided examples."""
        dataset = ReaderDataset(data, self.reader)
        sampler = SortedBatchSampler(
            dataset.lengths(),
            self.batch_size,
            shuffle=False
        )
        if self.reader_model != 'transformer':
            loader = torch.utils.data.DataLoader(
                dataset,
                batch_size=self.batch_size,
                sampler=sampler,
                num_workers=num_loaders,
                collate_fn=batchify,
                pin_memory=self.cuda,
            )
        else:
            loader = torch.utils.data.DataLoader(
                dataset,
                batch_size=self.batch_size,
                sampler=sampler,
                num_workers=num_loaders,
                collate_fn=batchify_transformer,
                pin_memory=self.cuda,
            )
        return loader

    def process(self, query, candidates=None, top_n=1, n_docs=5,
                return_context=False):
        """Run a single query."""
        predictions = self.process_batch(
            [query], [candidates] if candidates else None,
            top_n, n_docs, return_context
        )
        return predictions[0]

    def process_batch(self, queries, candidates=None, top_n=1, n_docs=5,
                      return_context=False):
        """Run a batch of queries (more efficient)."""
        t0 = time.time()
        logger.info('Processing %d queries...' % len(queries))
        logger.info('Retrieving top %d docs...' % n_docs)

        # Rank documents for queries.
        if len(queries) == 1:
            ranked = [self.ranker.closest_docs(queries[0], k=n_docs)]
        else:
            ranked = self.ranker.batch_closest_docs(
                queries, k=n_docs, num_workers=self.num_workers
            )
        all_docids, all_doc_scores = zip(*ranked)

        # Flatten document ids and retrieve text from database.
        # We remove duplicates for processing efficiency.
        flat_docids = list({d for docids in all_docids for d in docids})
        did2didx = {did: didx for didx, did in enumerate(flat_docids)}
        doc_texts = self.processes.map(fetch_text, flat_docids)

        # Split and flatten documents. Maintain a mapping from doc (index in
        # flat list) to split (index in flat list).
        flat_splits = []
        didx2sidx = []
        for text in doc_texts:
            splits = self._split_doc(text)
            didx2sidx.append([len(flat_splits), -1])
            for split in splits:
                flat_splits.append(split)
            didx2sidx[-1][1] = len(flat_splits)

        # Push through the tokenizers as fast as possible.
        q_tokens = self.processes.map_async(tokenize_text, queries)
        s_tokens = self.processes.map_async(tokenize_text, flat_splits)
        q_tokens = q_tokens.get()
        s_tokens = s_tokens.get()

        # Group into structured example inputs. Examples' ids represent
        # mappings to their question, document, and split ids.
        examples = []
        for qidx in range(len(queries)):
            for rel_didx, did in enumerate(all_docids[qidx]):
                start, end = didx2sidx[did2didx[did]]
                for sidx in range(start, end):
                    if self.reader_model == 'transformer':
                        if (len(q_tokens[qidx]) > 0 and 
                                len(s_tokens[sidx]) > 0):
                            examples.append({
                                'id': (qidx, rel_didx, sidx),
                                'question': q_tokens[qidx],
                                'document': s_tokens[sidx],
                            })
                    else:
                        if (len(q_tokens[qidx].words()) > 0 and
                                len(s_tokens[sidx].words()) > 0):
                            examples.append({
                                'id': (qidx, rel_didx, sidx),
                                'question': q_tokens[qidx].words(),
                                'qlemma': q_tokens[qidx].lemmas(),
                                'document': s_tokens[sidx].words(),
                                'lemma': s_tokens[sidx].lemmas(),
                                'pos': s_tokens[sidx].pos(),
                                'ner': s_tokens[sidx].entities(),
                            })

        logger.info('Reading %d paragraphs...' % len(examples))

        # Push all examples through the document reader.
        # We decode argmax start/end indices asychronously on CPU.
        result_handles = []
        # num_loaders = min(self.max_loaders, math.floor(len(examples) / 1e3))
        num_loaders = 0
        for batch in tqdm(self._get_loader(examples, num_loaders)):
            if candidates or self.fixed_candidates:
                batch_cands = []
                for ex_id in batch[-1]:
                    batch_cands.append({
                        'input': s_tokens[ex_id[2]],
                        'cands': candidates[ex_id[0]] if candidates else None
                    })
                handle = self.reader.predict(
                    batch, batch_cands, async_pool=None
                )
            else:
                handle = self.reader.predict(batch, async_pool=None)
            result_handles.append((handle, batch[-1], batch[0].size(0)))

        # Iterate through the predictions, and maintain priority queues for
        # top scored answers for each question in the batch.
        queues = [[] for _ in range(len(queries))]
        for result, ex_ids, batch_size in tqdm(result_handles):
            # s, e, score = result.get()
            s, e, score = result
            for i in range(batch_size):
                # We take the top prediction per split.
                if len(score[i]) > 0:
                    item = (score[i][0], ex_ids[i], s[i][0], e[i][0])
                    queue = queues[ex_ids[i][0]]
                    if len(queue) < top_n:
                        heapq.heappush(queue, item)
                    else:
                        heapq.heappushpop(queue, item)

        global BERT_TOKENIZER
        if BERT_TOKENIZER is None:
            from transformers import BertTokenizer
            BERT_TOKENIZER = BertTokenizer.from_pretrained('bert-base-uncased')

        # Arrange final top prediction data.
        all_predictions = []
        for queue in queues:
            predictions = []
            while len(queue) > 0:
                score, (qidx, rel_didx, sidx), s, e = heapq.heappop(queue)
                if self.reader_model != 'transformer':
                    prediction = {
                        'doc_id': all_docids[qidx][rel_didx],
                        'span': s_tokens[sidx].slice(s, e + 1).untokenize(),
                        'doc_score': float(all_doc_scores[qidx][rel_didx]),
                        'span_score': float(score),
                    }
                    if return_context:
                        prediction['context'] = {
                            'text': s_tokens[sidx].untokenize(),
                            'start': s_tokens[sidx].offsets()[s][0],
                            'end': s_tokens[sidx].offsets()[e][1],
                        }
                else:
                    sep_offset = len(q_tokens[qidx]) + 2
                    # logger.info(f'start: {s}, end: {end}, offset: {sep_offset}, seq: {s_tokens[sidx]}')
                    s = max(0, s - sep_offset)
                    e = max(0, e - sep_offset)
                    span = ' '.join(s_tokens[sidx][s:e+1])
                    prediction = {
                        'doc_id': all_docids[qidx][rel_didx],
                        'span': span,
                        'doc_score': float(all_doc_scores[qidx][rel_didx]),
                        'span_score': float(score),
                    }
                    if return_context:
                        all_tokens = s_tokens[sidx]
                        start_offset = len(' '.join(all_tokens[:s]))
                        end_offset = len(' '.join(all_tokens[:e + 1]))
                        prediction['context'] = {
                            'text': ' '.join(all_tokens),
                            'start': start_offset,
                            'end': end_offset,
                        }
                predictions.append(prediction)
            all_predictions.append(predictions[-1::-1])

        logger.info('Processed %d queries in %.4f (s)' %
                    (len(queries), time.time() - t0))

        return all_predictions
