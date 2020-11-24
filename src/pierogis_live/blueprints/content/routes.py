from flask import render_template

from . import content
from pierogis_live.models import Content


@content.route('', methods=['GET'])
def index():
    return "These are my projects"


@content.route('/<content>', methods=['GET'])
def content(content: str):
    content = Content(
        url='http://d3ds4cnxj62erj.cloudfront.net/oh~no!/ono.mp4',
        content_type = 'v',
        title="oh~no!"
    )
    return render_template('content/codename.html', content=content)
