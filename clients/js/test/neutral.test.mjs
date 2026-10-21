// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — essais : le format neutre d'ARKLIGHT ↔ OpenAI, dans les deux sens.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  fromOpenAIMessages,
  fromOpenAITools,
  imageDataUrl,
  parseToolArguments,
  toOpenAIMessages,
  toOpenAITools,
} from '../src/neutral.js';

const PHOTO = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ==';
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

// une conversation d'assistant comme ARKLIGHT la garde
const neutral = [
  { role: 'user', content: 'Place les projecteurs du dessin.', images: [PHOTO] },
  { role: 'assistant', content: '', toolCalls: [{ id: 't1', name: 'get_scene', args: {} }] },
  { role: 'tool', toolCallId: 't1', name: 'get_scene', content: '{"fixtures":[]}' },
  { role: 'assistant', content: 'Je place deux Titan.', toolCalls: [{ id: 't2', name: 'add_fixtures', args: { count: 2, model: 'titan' } }] },
  { role: 'tool', toolCallId: 't2', name: 'add_fixtures', content: '{"ok":true}' },
  { role: 'assistant', content: 'Deux Titan ajoutés.' },
];

test('neutre → OpenAI : images, appels d’outils, résultats', () => {
  const out = toOpenAIMessages({ system: 'Tu es l’assistant.', messages: neutral });
  assert.deepEqual(out, [
    { role: 'system', content: 'Tu es l’assistant.' },
    {
      role: 'user',
      content: [
        { type: 'text', text: 'Place les projecteurs du dessin.' },
        { type: 'image_url', image_url: { url: PHOTO } },
      ],
    },
    { role: 'assistant', content: null, tool_calls: [{ id: 't1', type: 'function', function: { name: 'get_scene', arguments: '{}' } }] },
    { role: 'tool', tool_call_id: 't1', content: '{"fixtures":[]}' },
    {
      role: 'assistant',
      content: 'Je place deux Titan.',
      tool_calls: [{ id: 't2', type: 'function', function: { name: 'add_fixtures', arguments: '{"count":2,"model":"titan"}' } }],
    },
    { role: 'tool', tool_call_id: 't2', content: '{"ok":true}' },
    { role: 'assistant', content: 'Deux Titan ajoutés.' },
  ]);
});

test('OpenAI → neutre, et l’aller-retour garde la conversation', () => {
  const back = fromOpenAIMessages(toOpenAIMessages({ system: 'Consigne', messages: neutral }));
  assert.equal(back.system, 'Consigne');
  assert.equal(back.messages.length, neutral.length);
  assert.deepEqual(back.messages[0], { role: 'user', content: 'Place les projecteurs du dessin.', images: [PHOTO] });
  assert.deepEqual(back.messages[1], { role: 'assistant', content: '', toolCalls: [{ id: 't1', name: 'get_scene', args: {}, raw: '{}' }] });
  assert.deepEqual(back.messages[2], { role: 'tool', toolCallId: 't1', content: '{"fixtures":[]}' });
  assert.deepEqual(back.messages[3].toolCalls[0].args, { count: 2, model: 'titan' });
  assert.deepEqual(back.messages[5], { role: 'assistant', content: 'Deux Titan ajoutés.' });
  // et retour : le même corps qu'au premier passage
  assert.deepEqual(toOpenAIMessages(back), toOpenAIMessages({ system: 'Consigne', messages: neutral }));
});

test('les images : data-URL ou octets, jamais une adresse ni un chemin', () => {
  assert.equal(imageDataUrl(PHOTO), PHOTO);
  assert.equal(imageDataUrl(PNG), `data:image/png;base64,${PNG.toString('base64')}`);
  assert.equal(imageDataUrl(new Uint8Array([0xff, 0xd8, 0xff, 0xe0])).slice(0, 23), 'data:image/jpeg;base64,');
  for (const bad of ['https://exemple.fr/plan.jpg', 'C:\\plans\\plan.jpg', 'file:///plan.jpg', 'data:text/plain;base64,AAAA', 42]) {
    assert.throws(() => imageDataUrl(bad), (e) => e.code === 'invalid_request' && e.param === 'images');
  }
  assert.throws(() => imageDataUrl(Buffer.from('pas une image')), (e) => e.code === 'invalid_request');
  // une image seule, sans texte
  assert.deepEqual(toOpenAIMessages({ messages: [{ role: 'user', content: '', images: [PNG] }] })[0].content, [
    { type: 'image_url', image_url: { url: `data:image/png;base64,${PNG.toString('base64')}` } },
  ]);
});

test('arguments d’outils : objet, texte JSON, vide, deux fois encodé, illisible', () => {
  assert.deepEqual(parseToolArguments({ a: 1 }), { args: { a: 1 }, raw: '{"a":1}' });
  assert.deepEqual(parseToolArguments('{"fixture": 3, "level": 80}'), { args: { fixture: 3, level: 80 }, raw: '{"fixture": 3, "level": 80}' });
  assert.deepEqual(parseToolArguments(''), { args: {}, raw: '' });
  assert.deepEqual(parseToolArguments('   '), { args: {}, raw: '   ' });
  assert.deepEqual(parseToolArguments(undefined), { args: {}, raw: '' });
  assert.deepEqual(parseToolArguments(JSON.stringify(JSON.stringify({ b: 2 }))).args, { b: 2 });
  assert.deepEqual(parseToolArguments('{"fixture": 3,'), { args: null, raw: '{"fixture": 3,' });
  assert.deepEqual(parseToolArguments('[1, 2]'), { args: null, raw: '[1, 2]' });
});

test('un appel d’outil illisible repart avec son texte d’origine', () => {
  const out = toOpenAIMessages({ messages: [{ role: 'assistant', content: '', toolCalls: [{ id: 'c1', name: 'set_light', args: null, raw: '{"fixture": 3,' }] }] });
  assert.equal(out[0].tool_calls[0].function.arguments, '{"fixture": 3,');
});

test('un résultat d’outil en objet est mis en JSON ; sans identifiant, refusé', () => {
  assert.deepEqual(toOpenAIMessages({ messages: [{ role: 'tool', toolCallId: 'x', content: { ok: true } }] })[0], { role: 'tool', tool_call_id: 'x', content: '{"ok":true}' });
  assert.throws(() => toOpenAIMessages({ messages: [{ role: 'tool', content: '{}' }] }), (e) => e.code === 'invalid_request');
  assert.throws(() => toOpenAIMessages({ messages: [{ role: 'robot', content: '?' }] }), (e) => e.code === 'invalid_request');
});

test('les messages déjà au format d’OpenAI passent tels quels', () => {
  const openai = [
    { role: 'user', content: [{ type: 'text', text: 'Bonjour' }] },
    { role: 'assistant', content: null, tool_calls: [{ id: 'a', type: 'function', function: { name: 'f', arguments: '{}' } }] },
    { role: 'tool', tool_call_id: 'a', content: '1' },
  ];
  assert.deepEqual(toOpenAIMessages({ messages: openai }), openai);
});

test('les outils, dans les deux sens', () => {
  const tools = [{ name: 'set_light', description: 'Règle un projecteur.', parameters: { type: 'object', properties: { level: { type: 'integer' } }, required: ['level'] } }];
  const openai = toOpenAITools(tools);
  assert.deepEqual(openai, [{ type: 'function', function: tools[0] }]);
  assert.deepEqual(fromOpenAITools(openai), tools);
  assert.deepEqual(toOpenAITools(openai), openai);
  assert.deepEqual(toOpenAITools([{ name: 'sans_parametres' }])[0].function.parameters, { type: 'object', properties: {} });
  assert.throws(() => toOpenAITools([{ description: 'sans nom' }]), (e) => e.code === 'invalid_request' && e.param === 'tools');
});
