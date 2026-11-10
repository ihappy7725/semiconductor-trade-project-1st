"""The workspace adapter must ship every asset it reads or injects."""
from pathlib import Path
import unittest

from flask import Flask, render_template
from minjung.AXPORT_widget._axport_semiconductor_widgets.sx_widgets import attach


class WidgetAssetTests(unittest.TestCase):
    def test_workspace_and_injected_assets_are_available(self):
        root = Path(__file__).resolve().parents[2]
        app = Flask(__name__, template_folder=str(root / 'templates'))
        app.config['TESTING'] = True

        @app.get('/app')
        def workspace():
            return render_template('workspace.html', upload_limit_mb=4,
                                   junhee_user={'id': 'test', 'email': 'test@example.com'},
                                   junhee_csrf='test')

        attach(app)
        client = app.test_client()
        response = client.get('/app')
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="sx-board"', response.get_data(as_text=True))
        for asset in ('widgets.css', 'widget-text.js', 'widgets.js'):
            with self.subTest(asset=asset):
                with client.get('/_axp_semiconductor/assets/' + asset) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.get_data())
