"""Compatibility installer for older pip/setuptools environments."""

from setuptools import find_packages, setup


setup(
    name="wechat-cs-parser",
    version="0.1.0",
    description="Local-first private-chat parser and human-reviewed customer-service workbench",
    python_requires=">=3.9",
    packages=find_packages(include=("wechat_cs*", "dashboard_integration*")),
    package_data={
        "wechat_cs": ["static/*.html", "static/*.js", "static/*.css", "static/*.svg"]
    },
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "wechat-cs=wechat_cs.__main__:main",
            "wechat-cs-api=wechat_cs.api:main",
        ]
    },
)
