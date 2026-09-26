from concurrent.futures import ThreadPoolExecutor


def save(client, token, **headers):
    return client.put('/v1/settings/ai', json={'trigger_text': 'a', 'reply_text': 'b'},
                      headers={'X-CSRF-Token': token, **headers})


def test_tabs_keep_old_and_refreshed_tokens(authenticated):
    client, old = authenticated
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.get('/v1/auth/csrf'), range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.headers['cache-control'] == 'no-store' for r in responses)
    tokens = {r.json()['csrf_token'] for r in responses}
    assert len(tokens) == 1
    new = tokens.pop()
    assert save(client, old).status_code == 200
    assert save(client, new).status_code == 200
    assert save(client, 'incorrect').status_code == 403
    assert save(client, '').status_code == 403
    assert save(client, new, Origin='https://untrusted.example').status_code == 403


def test_tokens_bound_to_session_and_logout(authenticated):
    client, old = authenticated
    previous = client.get('/v1/auth/csrf').json()['csrf_token']
    client.post('/v1/auth/login', json={'email': 'admin@example.com', 'password': 'password123'})
    current = client.get('/v1/auth/csrf').json()['csrf_token']
    assert previous != current
    assert save(client, previous).status_code == 403
    assert save(client, old).status_code == 403
    assert save(client, current).status_code == 200
    assert client.post('/v1/auth/logout', headers={'X-CSRF-Token': current}).status_code == 204
    assert save(client, current).status_code == 401
