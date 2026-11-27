"""Executable regression for the missing presentation action in conversation 1446."""
import importlib.util
from pathlib import Path
import unittest

p = Path(__file__).parent / 'payload/backend/app/reception_v3/reference_introduction.py'
spec = importlib.util.spec_from_file_location('reference_contract', p)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ContractTest(unittest.TestCase):
    def decision(self, text, **fields):
        return dict(action='reply', messages=[{'text': text}], **fields)

    def test_original_failure(self):
        text = '我們三到四月的桃花行程，小團是4～6人一團。先給您看「林芝桃花＋珠峰11日」的完整安排；另外也有不上珠峰的9日版本，少珠峰段兩天，您可以一起參考～'
        self.assertTrue(module.validation_error(self.decision(text), {}))

    def test_action_present(self):
        for fields in ({'start_introduction': True}, {'reference_introduction_route': 'peach_11d_2027'}):
            self.assertFalse(module.promised_introduction_without_action(self.decision('先給您看完整行程', **fields)))

    def test_not_a_present_full_introduction(self):
        for text in ('如果需要，我可以給您看完整行程', '上面已經給您看完整行程',
                     '稍後給您發完整行程', '給您介紹雙床房的安排', '11日有珠峰，9日不上珠峰',
                     '您只想看9日，我先回答您的問題', '好的，暫時不打擾您'):
            with self.subTest(text=text):
                self.assertFalse(module.promised_introduction_without_action(self.decision(text)))

    def test_simplified_and_traditional(self):
        for text in ('先给您看11天的完整行程', '現在給您介紹完整行程', '先給您看「桃花＋珠峰11日」的完整安排'):
            self.assertTrue(module.promised_introduction_without_action(self.decision(text)))


if __name__ == '__main__':
    unittest.main()
