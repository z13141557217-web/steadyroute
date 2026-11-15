import json
from test_netcoffee import ImportTests
import unittest

class TemplateTransaction(ImportTests):
    def test_toggle_round_trip(self):
        self.service.apply({'netcoffee':True})
        baseline=self.files()
        (self.service.base_dir/'service-template.local.json').write_text(json.dumps(dict(
            rules=['DOMAIN,allow.invalid,DIRECT','DOMAIN,deny.invalid,REJECT','MATCH,claude'],mapping={})))
        self.service.apply({'service_template':True})
        self.assertIn('deny.invalid',self.files()['clash-verge.yaml'])
        self.service.apply({'service_template':False})
        self.assertEqual(self.files(),baseline)
