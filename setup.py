import pathlib

from setuptools import find_packages, setup

package_name = "adt4_console"
here = pathlib.Path(__file__).parent


def static_files():
    """The built web UI (adt4_console/static/**, from ``npm run build`` in
    frontend/), shipped as package data when it has been built."""
    root = here / package_name / "static"
    return [
        str(p.relative_to(here / package_name)) for p in root.rglob("*") if p.is_file()
    ]


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["tests", "tests.*"]),
    package_data={package_name: static_files()},
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (
            "share/" + package_name + "/scripts/geo",
            [str(p) for p in sorted((here / "scripts" / "geo").glob("*.py"))],
        ),
    ],
    # colcon does not install pip packages: these are listed for pip users and
    # for documentation. In Awesome-DCIST-T4 they come from spark_env
    # (install/spark_requirements.txt).
    install_requires=["setuptools"],
    extras_require={
        "server": [
            "fastapi",
            "uvicorn[standard]",
            "pydantic>=2",
            "numpy",
            "msgpack",
            "scipy",
            "networkx",
            "pillow",
            "pyproj",
            "trimesh",
            "neo4j",
            "pyyaml",
            "pyzmq",
        ],
        "llm": ["openai", "anthropic"],
        "test": ["pytest", "httpx", "ruamel.yaml"],
        # scripts/geo: GPS anchors from bags and the satellite tile cache
        "geo": ["mcap", "rosbags", "rasterio", "matplotlib", "contextily"],
    },
    zip_safe=False,
    maintainer="Harel Biggie",
    maintainer_email="harelb@mit.edu",
    description="ADT4 web console: base-station server and browser UI",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "adt4-console = adt4_console.cli:main",
            "adt4-mission = adt4_console.mission_log.cli:main",
            "adt4-mission-slice = adt4_console.mission_log.slicer:main",
        ]
    },
)
