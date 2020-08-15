import pickle

import re
import torch
import os

from tqdm import tqdm
import optparse

from transformers import AutoTokenizer

from configs import TRANSFORMER_MODEL_NAME, DEVICE
from data_utils import read_surfaces, read_surface_lemma_map
from languages import NON_TRANSFORMER_LANGUAGES
from layers import EncoderRNN, DecoderRNN, TransformerRNN, DecoderFF
from logger import LOGGER
from train import embedding_size, char_gru_hidden_size, word_gru_hidden_size, encoder_dropout, device, \
    output_embedding_size, decoder_dropout

REMOVE_EOS_REGEX = re.compile(r'\$$')


def predict_sentence(surface_words, encoder, decoder_lemma, decoder_morph_tags, dataset,
                     use_transformer=True, use_char_lstm=True, transformer_model_name=TRANSFORMER_MODEL_NAME,
                     tokenizer=None, use_rnn_morph=False, device=DEVICE,
                     max_morph_features_len=10, surface2lemma=None):
    """

    Args:
        surface_words (list): List of tokens (str)
        encoder (`layers.EncoderRNN`): Encoder RNN
        decoder_lemma (`layers.TransformerRNN`): Lemma Decoder
        decoder_morph_tags (`layers.DecoderRNN`): Morphological Features Decoder
        dataset (`torch.utils.data.Dataset`): Train Dataset. Required for vocab etc.
        use_transformer (bool): indicates weather to use transformer
        use_char_lstm (bool): indicate whether to use char lstm
        transformer_model_name (str): HuggingFace style transformer name
        tokenizer (Tokenizer): HuggingFace style Tokenizer
        use_rnn_morph (str): indicate whether to use lstm to dcode morph tags
        device (`torch.device`): Default is cpu
        max_morph_features_len (int): Maximum length of morphological features
        surface2lemma (dict): Dictionary where keys are surface words and values are lemmas
            if surface exists in the dictionary, model prediction is ignored.
            Default is None

    Returns:
        str: Predicted conll sentence
    """

    if len(surface_words) == 0:
        return ""

    max_token_len = max([len(surface) for surface in surface_words]) + 1

    encoded_surfaces = torch.zeros((len(surface_words), max_token_len), dtype=torch.long)
    if use_char_lstm:
        for ix, surface in enumerate(surface_words):
            encoded_surface = dataset.encode(surface, dataset.surface_char2id)
            encoded_surfaces[ix, :encoded_surface.size()[0]] = encoded_surface

    encoded_surfaces = encoded_surfaces.to(device)

    sub_tokens = []
    word_ids = []
    if use_transformer:
        if tokenizer is None:
            tokenizer = AutoTokenizer.from_pretrained(transformer_model_name)
            tokenizer.basic_tokenizer.do_lower_case = False
        for word_id, surface in enumerate(surface_words):
            sub_tokens_ids = tokenizer.wordpiece_tokenizer.tokenize(surface[:-1])
            sub_tokens_ids = tokenizer.convert_tokens_to_ids(sub_tokens_ids)
            sub_tokens += sub_tokens_ids
            word_ids += [word_id] * len(sub_tokens_ids)
        sub_tokens, word_ids = torch.LongTensor(sub_tokens).to(device), torch.LongTensor(word_ids).to(device)


    # Run encoder
    transformer_output, word_representations, context_aware_representations = encoder(
        (sub_tokens.view(1, *sub_tokens.size()), word_ids.view(1, *sub_tokens.size())),
        encoded_surfaces.view(1, *encoded_surfaces.size()))

    # Run lemma decoder for each word
    words_count = context_aware_representations.size(0)

    if isinstance(decoder_lemma, TransformerRNN):
        if use_transformer:
            _, lemmas = decoder_lemma.predict(word_representations, context_aware_representations,
                                              encoded_surfaces.view(1, *encoded_surfaces.size()), surface_words,
                                              transformer_context=transformer_output)

        else:
            _, lemmas = decoder_lemma.predict(word_representations, context_aware_representations,
                                              encoded_surfaces.view(1, *encoded_surfaces.size()), surface_words)
    else:
        lemmas = []
        for i in range(words_count):
            if use_transformer:
                _, lemma = decoder_lemma.predict(word_representations[i], context_aware_representations[i],
                                                 max_len=2 * max_token_len, transformer_context=transformer_output[0][i],
                                                 device=device)
            else:
                _, lemma = decoder_lemma.predict(word_representations[i], context_aware_representations[i],
                                                 max_len=2 * max_token_len, device=device)
            lemmas.append(''.join(lemma))

    if surface2lemma:

        modified_lemmas = []
        for surface, lemma in zip(surface_words, lemmas):
            if isinstance(decoder_lemma, TransformerRNN):
                _surface = surface[:-1]
            else:
                _surface = surface
            if _surface in surface2lemma and surface2lemma[_surface] != lemma:
                modified_lemmas.append(surface2lemma[_surface])
                # print('Changing {} to {}'.format(lemma, surface2lemma[_surface]))
            else:
                modified_lemmas.append(lemma)
        lemmas = modified_lemmas

    # Run morph features decoder for each word
    morph_features = []

    for i in range(words_count):
        if use_transformer and use_rnn_morph:
            _, morph_feature = decoder_morph_tags.predict(word_representations[i],
                                                          context_aware_representations[i],
                                                          transformer_context=transformer_output[0][i],
                                                          max_len=max_morph_features_len, device=device)
        elif use_transformer:
            morph_feature = decoder_morph_tags.predict(word_representations[i], context_aware_representations[i],
                                                       transformer_context=transformer_output[0][i])
        elif use_rnn_morph:
            _, morph_feature = decoder_morph_tags.predict(word_representations[i],
                                                          context_aware_representations[i],
                                                          max_len=max_morph_features_len, device=device)
        else:
            morph_feature = decoder_morph_tags.predict(word_representations[i], context_aware_representations[i])

        morph_features.append(';'.join(morph_feature))

    conll_sentence = "# Sentence\n"
    for i, (surface, lemma, morph_feature) in enumerate(zip(surface_words, lemmas, morph_features)):
        conll_sentence += "{}\t{}\t{}\t_\t_\t{}\t_\t_\t_\t_\n".format(i + 1,
                                                                      REMOVE_EOS_REGEX.sub('', surface),
                                                                      lemma, morph_feature)
    return conll_sentence


def predict(input_file, output_file, dataset_obj_path, encoder_model_path, lemma_decoder_path, morph_decoder_path,
            use_transformer=True, use_char_lstm=True, transformer_model_name=TRANSFORMER_MODEL_NAME):
    """

    Args:
        input_file (str): Input conll file path. Tab separated format. Second column contains surface words.
            Other columns are ignored, could be empty. Total 10 columns.
        output_file (str): Output conll file path. Second column contains surface words.
            Third column contains lemmata and sixth column contains morphological tags.
        dataset_obj_path (str): The path of the dataset object which is saved during training process
        encoder_model_path (str): The path of the encoder model which is saved during training process
        lemma_decoder_path (str): The path of the lemma decoder model which is saved during training process
        morph_decoder_path (str): The path of the morph decoder model which is saved during training process
        use_transformer (bool): indicates weather to use transformer
        use_char_lstm (bool): indicate whether to use char lstm
        transformer_model_name (str): HuggingFace style transformer name
    """

    LOGGER.info('Loading dataset obj...')
    with open(dataset_obj_path, 'rb') as f:
        train_set = pickle.load(f)

    tokenizer = None
    if use_transformer:
        tokenizer = AutoTokenizer.from_pretrained(transformer_model_name)
        tokenizer.basic_tokenizer.do_lower_case = False

    # LOAD ENCODER MODEL
    LOGGER.info('Loading Encoder...')

    if use_transformer:
        encoder = EncoderRNN(embedding_size, char_gru_hidden_size, word_gru_hidden_size,
                             len(train_set.surface_char2id), TRANSFORMER_MODEL_NAME,
                             dropout_ratio=encoder_dropout, device=device)
    else:
        encoder = EncoderRNN(embedding_size, char_gru_hidden_size, word_gru_hidden_size,
                             len(train_set.surface_char2id), None,
                             dropout_ratio=encoder_dropout, device=device)

    encoder.load_state_dict(torch.load(encoder_model_path))
    encoder = encoder.to(device)

    # LOAD LEMMA DECODER MODEL
    LOGGER.info('Loading Lemma Decoder...')

    if use_transformer:
        decoder_lemma = TransformerRNN(output_embedding_size, word_gru_hidden_size, train_set.transformation2id,
                                       len(train_set.surface_char2id), layer_size=3,
                                       dropout_ratio=decoder_dropout).to(device)
    else:
        decoder_lemma = TransformerRNN(output_embedding_size, word_gru_hidden_size, train_set.transformation2id,
                                       len(train_set.surface_char2id), layer_size=3,
                                       dropout_ratio=decoder_dropout).to(device)

    decoder_lemma.load_state_dict(torch.load(lemma_decoder_path))
    decoder_lemma = decoder_lemma.to(device)

    # LOAD MORPH DECODER MODEL
    LOGGER.info('Loading Morph Decoder...')

    if use_transformer:
        decoder_morph_tags = DecoderRNN(output_embedding_size, word_gru_hidden_size, train_set.morph_tag2id,
                                        layer_size=3, dropout_ratio=decoder_dropout).to(device)
    else:
        decoder_morph_tags = DecoderRNN(output_embedding_size, word_gru_hidden_size, train_set.morph_tag2id,
                                        layer_size=2, dropout_ratio=decoder_dropout).to(device)

    decoder_morph_tags.load_state_dict(torch.load(morph_decoder_path))
    decoder_morph_tags = decoder_morph_tags.to(device)

    encoder.eval()
    decoder_lemma.eval()
    decoder_morph_tags.eval()

    # Make predictions and save to file
    data_surface_words = read_surfaces(input_file)
    with open(output_file, 'w', encoding='UTF-8') as f:
        for sentence in tqdm(data_surface_words):
            conll_sentence = predict_sentence(sentence, encoder, decoder_lemma, decoder_morph_tags,
                                              train_set, use_transformer=use_transformer, use_char_lstm=use_char_lstm,
                                              transformer_model_name=transformer_model_name, tokenizer=tokenizer,
                                              device=device, surface2lemma=dict())
            f.write(conll_sentence)
            f.write('\n')


def predict_unimorph(language_path, model_name, conll_file, use_surface_lemma_mapping=True,
                     prediction_file=None, use_min_edit_operation_decoder=True,
                     use_transformer=True, use_char_lstm=True, use_rnn_morph=False,
                     transformer_model_name=TRANSFORMER_MODEL_NAME):

    language_conll_files = os.listdir(language_path)
    for language_conll_file in language_conll_files:
        if 'train.' in language_conll_file:

            # LOAD DATASET
            LOGGER.info('Loading dataset...')
            train_data_path = language_path + '/' + language_conll_file

            tokenizer = None
            if use_transformer:
                tokenizer = AutoTokenizer.from_pretrained(transformer_model_name)
                tokenizer.basic_tokenizer.do_lower_case = False

            surface2lemma = None
            if use_surface_lemma_mapping:
                surface2lemma = read_surface_lemma_map(train_data_path)
                print('Surface Lemma Mapping Length: {}'.format(len(surface2lemma)))

            with open(train_data_path.replace('-train', '').replace('conllu', '{}.dataset'.format(model_name)),
                      'rb') as f:
                train_set = pickle.load(f)
            if any([l in language_path for l in NON_TRANSFORMER_LANGUAGES]):
                add_eos = False
            else:
                add_eos = True
            if language_path in conll_file:
                data_surface_words = read_surfaces(conll_file, add_eos=add_eos)
            else:
                data_surface_words = read_surfaces(language_path + '/' + conll_file, add_eos=add_eos)

            # LOAD ENCODER MODEL
            LOGGER.info('Loading Encoder...')
            if use_transformer:
                encoder = EncoderRNN(embedding_size, char_gru_hidden_size, word_gru_hidden_size,
                                     len(train_set.surface_char2id), TRANSFORMER_MODEL_NAME,
                                     dropout_ratio=encoder_dropout, device=device)
            else:
                encoder = EncoderRNN(embedding_size, char_gru_hidden_size, word_gru_hidden_size,
                                     len(train_set.surface_char2id), None,
                                     dropout_ratio=encoder_dropout, device=device)
            encoder.load_state_dict(torch.load(
                train_data_path.replace('train', 'encoder').replace('conllu', '{}.model'.format(model_name))
            ))
            encoder = encoder.to(device)

            # LOAD LEMMA DECODER MODEL
            LOGGER.info('Loading Lemma Decoder...')

            if any([l in language_path for l in NON_TRANSFORMER_LANGUAGES]) or not use_min_edit_operation_decoder:
                if use_transformer:
                    decoder_lemma = DecoderRNN(output_embedding_size, word_gru_hidden_size, train_set.lemma_char2id,
                                               layer_size=3, dropout_ratio=decoder_dropout).to(device)
                else:
                    decoder_lemma = DecoderRNN(output_embedding_size, word_gru_hidden_size, train_set.lemma_char2id,
                                               layer_size=2, dropout_ratio=decoder_dropout).to(device)
            else:
                if use_transformer:
                    decoder_lemma = TransformerRNN(output_embedding_size, word_gru_hidden_size,
                                                   train_set.transformation2id,
                                                   len(train_set.surface_char2id), layer_size=3,
                                                   dropout_ratio=decoder_dropout).to(device)
                else:
                    decoder_lemma = TransformerRNN(output_embedding_size, word_gru_hidden_size,
                                                   train_set.transformation2id,
                                                   len(train_set.surface_char2id), layer_size=3,
                                                   dropout_ratio=decoder_dropout).to(device)

            decoder_lemma.load_state_dict(torch.load(
                train_data_path.replace('train', 'decoder_lemma').replace('conllu', '{}.model'.format(model_name))
            ))
            decoder_lemma = decoder_lemma.to(device)

            # LOAD MORPH DECODER MODEL
            LOGGER.info('Loading Morph Decoder...')

            if use_transformer and use_rnn_morph:
                decoder_morph_tags = DecoderRNN(output_embedding_size, word_gru_hidden_size, train_set.morph_tag2id,
                                                layer_size=3, dropout_ratio=decoder_dropout).to(device)
            elif use_rnn_morph:
                decoder_morph_tags = DecoderRNN(output_embedding_size, word_gru_hidden_size, train_set.morph_tag2id,
                                                layer_size=2, dropout_ratio=decoder_dropout).to(device)
            else:
                decoder_morph_tags = DecoderFF(word_gru_hidden_size, train_set.morph_tag2id,
                                               dropout_ratio=decoder_dropout).to(device)

            decoder_morph_tags.load_state_dict(torch.load(
                train_data_path.replace('train', 'decoder_morph').replace('conllu', '{}.model'.format(model_name))
            ))
            decoder_morph_tags = decoder_morph_tags.to(device)

            encoder.eval()
            decoder_lemma.eval()
            decoder_morph_tags.eval()

            # Make predictions and save to file
            if not prediction_file:
                prediction_file = train_data_path.replace('train', 'predictions-{}'.format(model_name))
            with open(prediction_file, 'w', encoding='UTF-8') as f:
                for sentence in tqdm(data_surface_words):
                    conll_sentence = predict_sentence(sentence, encoder, decoder_lemma, decoder_morph_tags,
                                                      train_set, use_transformer=use_transformer,
                                                      use_char_lstm=use_char_lstm,
                                                      transformer_model_name=transformer_model_name,
                                                      tokenizer=tokenizer,
                                                      device=device, surface2lemma=surface2lemma)
                    f.write(conll_sentence)
                    f.write('\n')


if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.set_description(
        'Run given model on a given dataset. Predicts lemmata and morphological tags for each word in the dataset')

    parser.add_option('-i', '--input_file',
                      action='store', dest='input_file',
                      help='Input CONLL file path')
    parser.add_option('-o', '--output_file',
                      action='store', dest='output_file',
                      help='Output file path')
    parser.add_option('-d', '--dataset_obj_file',
                      action='store', dest='dataset_obj_file',
                      help='The path of the dataset object which is saved during training process')

    parser.add_option('-e', '--encoder_file',
                      action='store', dest='encoder_file',
                      help='The path of the encoder object which is saved during training process')

    parser.add_option('-l', '--lemma_decoder_file',
                      action='store', dest='lemma_decoder_file',
                      help='The path of the lemma decoder object which is saved during training process')

    parser.add_option('-m', '--morph_decoder_file',
                      action='store', dest='morph_decoder_file',
                      help='The path of the morph decoder object which is saved during training process')

    options, args = parser.parse_args()
    if all([options.input_file, options.output_file, options.dataset_obj_file,
            options.encoder_file, options.lemma_decoder_file, options.morph_decoder_file]):
        predict(options.input_file, options.output_file, options.dataset_obj_file,
                options.encoder_file, options.lemma_decoder_file, options.morph_decoder_file)
    else:
        parser.print_help()

