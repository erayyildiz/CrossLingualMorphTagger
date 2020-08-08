from itertools import chain

import torch
from torch.utils.data import Dataset
from transformers import AutoTokenizer

from data_utils import read_dataset


class ConllDataset(Dataset):
    """Torch dataset for sigmorphon conll data"""

    PAD_token = '<p>'
    EOS_token = '<e>'
    START_TAG = '<s>'

    def __init__(self, conll_file_path, surface_char2id=None, lemma_char2id=None, morph_tag2id=None,
                 transformation2id=None, transformer_model_name=None, mode='train',  max_sentences=0):
        """Initialize ConllDataset.

        Arguments:
            conll_file_path (str): conll file path
            surface_char2id (dict): Default is None. if None calculated over given data
            lemma_char2id (dict): Default is None. if None calculated over given data
            morph_tag2id (dict): Default is None. if None calculated over given data
            transformation2id (dict): Default is None. if None calculated over given data
            transformer_model_name (string): uggingFace style name of the transformer model if used
            mode (str): 'train' or 'test'. If 'test' vocab dicts will not be updated
            max_sentences (int): Maximum number of sentences to be loaded into dataset.
                Default is 0 which means no limitation
        """
        self.sentences = read_dataset(conll_file_path)
        if 0 < max_sentences < len(self.sentences):
            self.sentences = self.sentences[:max_sentences]
        if surface_char2id:
            self.surface_char2id = surface_char2id
        else:
            self.surface_char2id = dict()
            self.surface_char2id[self.PAD_token] = len(self.surface_char2id)
            self.surface_char2id[self.EOS_token] = len(self.surface_char2id)
        if lemma_char2id:
            self.lemma_char2id = lemma_char2id
        else:
            self.lemma_char2id = dict()
            self.lemma_char2id[self.PAD_token] = len(self.lemma_char2id)
            self.lemma_char2id[self.EOS_token] = len(self.lemma_char2id)
            self.lemma_char2id[self.START_TAG] = len(self.lemma_char2id)

        if transformation2id:
            self.transformation2id = transformation2id
        else:
            self.transformation2id = dict()
            self.transformation2id[self.PAD_token] = len(self.transformation2id)

        if morph_tag2id:
            self.morph_tag2id = morph_tag2id
        else:
            self.morph_tag2id = dict()
            self.morph_tag2id[self.PAD_token] = len(self.morph_tag2id)
            self.morph_tag2id[self.EOS_token] = len(self.morph_tag2id)
            self.morph_tag2id[self.START_TAG] = len(self.morph_tag2id)

        self.transformer_model_name = transformer_model_name
        self.tokenizer = None
        if self.transformer_model_name:
            self.tokenizer = AutoTokenizer.from_pretrained(self.transformer_model_name)
            self.tokenizer.basic_tokenizer.do_lower_case = False
        self.mode = mode
        if mode == 'train':
            self.create_vocabs()

    def create_vocabs(self):
        """Create surface_char2id, lemma_char2id and morph_tag2id vocabs using provided data

        """
        print('Creating vocabs...')

        # Update surface_char2id
        unique_surfaces = set(chain(*[sentence.surface_words for sentence in self.sentences]))
        unique_chars = set(chain(*[surface for surface in unique_surfaces]))
        for ch in unique_chars:
            self.surface_char2id[ch] = len(self.surface_char2id)

        # Update lemma_char2id
        unique_lemmas = set(chain(*[sentence.lemmas for sentence in self.sentences]))
        unique_chars = set(chain(*[lemma for lemma in unique_lemmas]))
        for ch in unique_chars:
            self.lemma_char2id[ch] = len(self.lemma_char2id)

        # Update transformation2id
        for sentence in self.sentences:
            for transformation in sentence.transformations:
                for _t in transformation:
                    if _t not in self.transformation2id:
                        self.transformation2id[_t] = len(self.transformation2id)

        # Update morph_tag2id
        unique_morph_tags = list(chain(*[sentence.morph_tags for sentence in self.sentences]))
        unique_tags = set(chain(*[morph_tag for morph_tag in unique_morph_tags]))
        for tag in unique_tags:
            self.morph_tag2id[tag] = len(self.morph_tag2id)
        print('Surface Chars={}, Lemma Chars={}, Transformations={}, tags={}'.format(
            len(self.surface_char2id), len(self.lemma_char2id), len(self.transformation2id), len(self.morph_tag2id)
        ))


    @staticmethod
    def encode(seq, vocab, add_start_tag=False, add_end_tag=True):
        res = []
        if add_start_tag:
            res.append(vocab[ConllDataset.START_TAG])
        for token in seq:
            if token in vocab:
                res.append(vocab[token])
        if add_end_tag:
            res.append(vocab[ConllDataset.EOS_token])
        return torch.tensor(res, dtype=torch.long)

    def __len__(self):
        return len(self.sentences)

    def __getitem__(self, index):
        sentence = self.sentences[index]
        max_token_len = max([len(surface)+1 for surface in sentence.surface_words])
        max_lemma_len = max([len(lemma)+2 for lemma in sentence.lemmas])
        max_morph_tags_len = max([len(morph_tag)+2 for morph_tag in sentence.morph_tags])

        # Encode surfaces
        encoded_surfaces = torch.zeros((len(sentence), max_token_len), dtype=torch.long)
        for ix, surface in enumerate(sentence.surface_words):
            encoded_surface = self.encode(surface, self.surface_char2id)
            encoded_surfaces[ix, :encoded_surface.size()[0]] = encoded_surface

        # Encode lemmas
        encoded_lemmas = torch.zeros((len(sentence), max_lemma_len), dtype=torch.long)
        for ix, lemma in enumerate(sentence.lemmas):
            encoded_lemma = self.encode(lemma, self.lemma_char2id, add_start_tag=True)
            encoded_lemmas[ix, :encoded_lemma.size()[0]] = encoded_lemma

        # Encode surfaces
        encoded_morph_tags = torch.zeros((len(sentence), max_morph_tags_len), dtype=torch.long)
        for ix, morph_tag in enumerate(sentence.morph_tags):
            encoded_morph_tag = self.encode(morph_tag, self.morph_tag2id, add_start_tag=True)
            encoded_morph_tags[ix, :encoded_morph_tag.size()[0]] = encoded_morph_tag

        # Encode transformations
        encoded_transformations = torch.zeros((len(sentence), max_token_len), dtype=torch.long)
        for ix, transformation in enumerate(sentence.transformations):
            encoded_transformation = self.encode(transformation, self.transformation2id,
                                                 add_start_tag=False, add_end_tag=False)
            encoded_transformations[ix, :encoded_transformation.size()[0]] = encoded_transformation

        encoded_surfaces_pretrained = []
        if self.transformer_model_name:
            sub_tokens = []
            word_ids = []
            for word_id, surface in enumerate(sentence.surface_words):
                sub_tokens_ids = self.tokenizer.wordpiece_tokenizer.tokenize(surface[:-1])
                sub_tokens_ids = self.tokenizer.convert_tokens_to_ids(sub_tokens_ids)
                sub_tokens += sub_tokens_ids
                word_ids += [word_id] * len(sub_tokens_ids)
            encoded_surfaces_pretrained = (torch.LongTensor(sub_tokens), torch.LongTensor(word_ids))

        return encoded_surfaces_pretrained, encoded_surfaces, encoded_lemmas, encoded_morph_tags, encoded_transformations