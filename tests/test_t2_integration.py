import json
import pytest
import psycopg
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from testcontainers.postgres import PostgresContainer
from applypilot import db, facts_repo, search
from applypilot.api import create_app
from applypilot.schemas import Fact, JobRequirements

pytestmark = pytest.mark.integration

@pytest.fixture(scope='module')
def database():
    with PostgresContainer('pgvector/pgvector:pg16') as pg:
        dsn = pg.get_connection_url().replace('postgresql+psycopg2://', 'postgresql://')
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn

@pytest.fixture
def client(database):
    with db.connect(database) as conn:
        conn.execute('TRUNCATE facts CASCADE')
    with TestClient(create_app(dsn=database, checkpointer=MemorySaver())) as c:
        yield c

def create_fact(c, **kwargs):
    r=c.post('/api/facts', json=dict(fact_type='project', source_name='Example', content='Python tool', skills=['Python'], **kwargs))
    assert r.status_code == 201, r.text
    return r.json()

def test_revision_confirmation_and_stale_conflicts(client, database):
    f=create_fact(client)
    assert f['revision']==1 and f['status']=='draft'
    url='/api/facts/'+f['id']
    confirmed=client.post(url+'/confirm', json={'expected_revision':1}).json()
    assert confirmed['revision']==2 and confirmed['status']=='confirmed'
    assert client.put(url,json={'expected_revision':1,'content':'wrong'}).status_code==409
    edited=client.put(url,json={'expected_revision':2,'content':'Updated tool'}).json()
    assert edited['status']=='draft' and edited['revision']==3
    assert client.post(url+'/confirm',json={'expected_revision':2}).status_code==409
    history=client.get(url+'/revisions').json()
    assert [r['status'] for r in history]==['draft','confirmed','draft']
    assert history[0]['content']=='Python tool'
    with db.connect(database) as conn:
        assert search.fulltext_hits(conn,['Updated'])=={}
        with pytest.raises(psycopg.Error):
            conn.execute('UPDATE fact_revisions SET snapshot=\'{}\'')
    assert client.put(url,json={'expected_revision':3,'content':'  '}).status_code==422
    assert client.put(url,json={'expected_revision':3,'status':'confirmed'}).status_code==422

def test_profile_and_linked_education_history(client):
    r=client.put('/api/profile',json={'expected_revision':0,'profile':{'name':'Sample Person','email':'example@example.invalid'}})
    assert r.status_code==200, r.text
    assert r.json()['status']=='draft'
    assert client.post('/api/profile/confirm',json={'expected_revision':1}).json()['status']=='confirmed'
    assert client.put('/api/profile',json={'expected_revision':1,'profile':{'name':'Stale'}}).status_code==409
    assert client.put('/api/profile',json={'expected_revision':2,'profile':{'name':'Updated'}}).json()['status']=='draft'
    assert len(client.get('/api/profile/revisions').json())==3
    edu=client.post('/api/facts',json={'fact_type':'education','source_name':'Example University','content':'Computer Science degree','school':'Example University','major':'Computer Science','degree':'Bachelor'}).json()
    assert client.get('/api/profile').json()['education'][0]['id']==edu['id']
    assert client.get('/profile').status_code==200

def test_education_outside_topk_and_embedding_invalidation(client,database):
    f=create_fact(client)
    client.post('/api/facts/'+f['id']+'/confirm',json={'expected_revision':1})
    edu=client.post('/api/facts',json={'fact_type':'education','source_name':'Example University','content':'Bachelor'}).json()
    client.post('/api/facts/'+edu['id']+'/confirm',json={'expected_revision':1})
    with db.connect(database) as conn:
        result=search.PostgresFactRetriever(conn,top_k=0)(JobRequirements())
        assert [r.id for r in result]==[edu['id']]
        vec=[1.0]+[0.0]*511
        class Provider:
            def embed(self,text): return vec
        search.backfill_embeddings(conn,Provider())
        assert f['id'] in search.vector_hits(conn,vec)
        client.put('/api/facts/'+f['id'],json={'expected_revision':2,'skills':['Rust']})
        assert conn.execute('SELECT embedding FROM facts WHERE id=%s',(f['id'],)).fetchone()['embedding'] is None
        client.post('/api/facts/'+f['id']+'/confirm',json={'expected_revision':3})
        class RacingProvider:
            def embed(self,text):
                client.put('/api/facts/'+f['id'],json={'expected_revision':4,'content':'Concurrent edit'})
                return vec
        assert search.backfill_embeddings(conn,RacingProvider())==0
        assert conn.execute('SELECT embedding FROM facts WHERE id=%s',(f['id'],)).fetchone()['embedding'] is None

def test_migration_preserves_legacy_as_draft_and_is_idempotent():
    with PostgresContainer('pgvector/pgvector:pg16') as pg:
        dsn=pg.get_connection_url().replace('postgresql+psycopg2://','postgresql://')
        with db.connect(dsn) as conn:
            conn.execute(db.SCHEMA_PATH.read_text(encoding='utf-8'))
            conn.execute("INSERT INTO facts(id,fact_type,source_name,content) VALUES ('legacy','project','Example','Old fact')")
            with TestClient(create_app(dsn=dsn,checkpointer=MemorySaver())) as c:
                assert c.get('/health/ready').status_code==503
            conn.execute("UPDATE facts SET embedding=%s::vector WHERE id='legacy'", (str([1.0] + [0.0]*511),))
            db.migrate(conn)
            db.migrate(conn)
            assert conn.execute("SELECT embedding FROM facts WHERE id='legacy'").fetchone()['embedding'] is None
            fact=facts_repo.get_fact(conn,'legacy')
            assert fact.content=='Old fact' and fact.status=='draft' and fact.origin=='legacy'
            assert len(facts_repo.fact_history(conn,'legacy'))==1
            assert search.fulltext_hits(conn,['Old'])=={}



def test_education_does_not_consume_experience_topk(client, database):
    from applypilot.schemas import KeywordRequirement
    project = create_fact(client)
    client.post('/api/facts/'+project['id']+'/confirm', json={'expected_revision': 1})
    education = client.post('/api/facts', json={
        'fact_type': 'education', 'source_name': 'School', 'content': 'Python Rust',
        'skills': ['Python', 'Rust'],
    }).json()
    client.post('/api/facts/'+education['id']+'/confirm', json={'expected_revision': 1})
    with db.connect(database) as conn:
        result = search.PostgresFactRetriever(conn, top_k=1)(JobRequirements(
            keywords=[KeywordRequirement(term='Python', importance='required'),
                      KeywordRequirement(term='Rust', importance='required')],
        ))
    assert {fact.id for fact in result} == {project['id'], education['id']}
