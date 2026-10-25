import base64,time
from io import BytesIO
import httpx,pytest,respx
from PIL import Image
from app.reception_v3.customer_images import download_image, MAX_BYTES

SOURCE='https://app.chatwoot.com/rails/active_storage/blobs/redirect/test/thumb.png'
STORE='https://prod-chatwoot-assets.s3.amazonaws.com/test/image'

def jpeg():
    stream=BytesIO();Image.new('RGB',(72,72),'blue').save(stream,format='JPEG');return stream.getvalue()

def test_download_follows_known_storage_and_uses_actual_format():
    data=jpeg()
    with respx.mock() as router:
        router.get(SOURCE).respond(302,headers={'location':STORE})
        router.get(STORE).respond(200,content=data,headers={'content-type':'image/png'})
        result=download_image(SOURCE,time.monotonic()+12)
    assert result=='data:image/jpeg;base64,'+base64.b64encode(data).decode()

@pytest.mark.parametrize('target',['https://evil.com/p.png','http://127.0.0.1/image','https://app.chatwoot.com/admin',
 'https://prod-chatwoot-assets.s3.amazonaws.com.evil.com/a'])
def test_rejects_untrusted_redirect_without_requesting_it(target):
    with respx.mock() as router:
        route=router.get(SOURCE).respond(302,headers={'location':target})
        with pytest.raises(ValueError,match='image_redirect_not_allowed'):download_image(SOURCE,time.monotonic()+12)
        assert route.call_count==1 and len(router.calls)==1

def test_image_byte_limit_enforced_without_trusting_header():
    with respx.mock() as router:
        router.get(SOURCE).respond(200,content=b'x'*(MAX_BYTES+1))
        with pytest.raises(ValueError,match='image_too_large'):download_image(SOURCE,time.monotonic()+12)

def test_expired_deadline_prevents_network_request():
    with respx.mock() as router:
        with pytest.raises(TimeoutError):download_image(SOURCE,time.monotonic()-1)
        assert len(router.calls)==0
